"""Turn a share/page URL into a direct downloadable link."""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import parse_qs, quote, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from .errors import InvalidUrl, NoFallbackResolutionError, ResolutionError
from .utils import (
    filename_from_headers,
    filename_from_url,
    is_magnet,
    normalize_url,
    sanitize_filename,
    validate_http_url,
)

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

GOFILE_API = "https://api.gofile.io"
GOFILE_LANG = "en-US"
# Salt baked into Gofile's /js/wt.obf.js. It can be overridden if Gofile rotates it.
GOFILE_WT_SALT = os.getenv("GOFILE_WT_SALT", "12af056dacea0b").strip() or "12af056dacea0b"
GOFILE_TOKEN_TTL = 6 * 60 * 60

_gofile_token_lock = threading.Lock()
_gofile_token: Optional[tuple[str, float]] = None


class _GofileAuthError(Exception):
    """The cached Gofile guest token was rejected."""


DOWNLOAD_WORDS = re.compile(
    r"(download|descargar|t[eé]l[eé]charger|herunterladen|скачать|下载|下載|تحميل|"
    r"unduh|muat turun|baixar|scarica|indir|stiahnu|let[oö]lt[eé]s|pobierz)",
    re.IGNORECASE,
)
FILE_EXT = re.compile(
    r"\.(zip|rar|7z|tar|gz|bz2|xz|iso|mp4|mkv|avi|mov|webm|mp3|flac|m4a|wav|pdf|"
    r"epub|apk|exe|msi|dmg|torrent|bin|img|docx?|xlsx?|pptx?|csv|txt|srt|vtt|"
    r"jpe?g|png|gif|webp)$",
    re.IGNORECASE,
)
BAD_HREF = re.compile(r"^(#|javascript:|mailto:|tel:|data:)", re.IGNORECASE)
ASSET_EXT = re.compile(r"\.(css|js|mjs|json|xml|woff2?|ttf|otf|eot|svg|ico|map|webmanifest)$", re.IGNORECASE)
WEB_ASSET_TYPES = {
    "text/css",
    "text/javascript",
    "application/javascript",
    "application/x-javascript",
    "application/font-woff",
    "font/woff",
    "font/woff2",
    "image/svg+xml",
}
VIDEO_SITE = re.compile(
    r"(^|\.)(youtube\.com|youtu\.be|vimeo\.com|dailymotion\.com|tiktok\.com|"
    r"twitter\.com|x\.com|instagram\.com|facebook\.com|fb\.watch|reddit\.com|"
    r"twitch\.tv|soundcloud\.com|bilibili\.com|rumble\.com|odysee\.com|"
    r"streamable\.com|v\.redd\.it|pornhub\.com|xvideos\.com)$",
    re.IGNORECASE,
)

TORRENT_TYPES = {"application/x-bittorrent", "application/x-bittorrent; charset=utf-8"}
HTML_TYPES = {"text/html", "application/xhtml+xml"}


@dataclass(slots=True)
class ResolvedLink:
    url: str
    filename: Optional[str] = None
    referer: Optional[str] = None
    content_type: Optional[str] = None
    size: Optional[int] = None
    kind: str = "http"  # http | torrent | magnet | ytdlp
    headers: Optional[dict[str, str]] = None
    parts: list[ResolvedLink] = field(default_factory=list)
    allow_ytdlp_fallback: bool = True
    force_download: bool = False


@dataclass(slots=True)
class _Probe:
    link: Optional[ResolvedLink] = None
    html: bool = False


class Resolver:
    """Resolve pages that hide the file behind a download button."""

    def __init__(
        self,
        *,
        timeout: tuple[int, int] = (15, 30),
        max_html_bytes: int = 3_000_000,
        allow_private_urls: bool = False,
        gofile_token: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.timeout = timeout
        self.max_html_bytes = max_html_bytes
        self.allow_private_urls = allow_private_urls
        self.gofile_token = (gofile_token or "").strip() or None
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    # ------------------------------------------------------------------ public
    def resolve(self, url: str, depth: int = 0) -> ResolvedLink:
        url = normalize_url(url)
        if is_magnet(url):
            return ResolvedLink(url=url, kind="magnet")

        if not self.allow_private_urls:
            validate_http_url(url)

        gofile_code = _gofile_code(url)
        if gofile_code:
            return self._resolve_gofile(gofile_code)

        url = self._special_url(url)
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        if VIDEO_SITE.search(host):
            return ResolvedLink(url=url, kind="ytdlp", filename=None)

        if depth > 2:
            raise ResolutionError("Too many redirect pages while looking for the file.")

        try:
            response = self.session.get(
                url,
                stream=True,
                allow_redirects=True,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ResolutionError(f"Could not open that link: {exc}") from exc

        try:
            status = response.status_code
            if status >= 400:
                raise ResolutionError(f"The server returned HTTP {status} for that link.")

            content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            final_url = response.url
            size = _content_length(response.headers)
            disposition = (response.headers.get("content-disposition") or "").lower()
            is_attachment = "attachment" in disposition

            redirect_code = _gofile_code(final_url)
            if redirect_code:
                return self._resolve_gofile(redirect_code)

            # Skip website assets found while scanning a page, but allow a
            # direct link to any file type the user explicitly sent.
            if content_type in WEB_ASSET_TYPES and depth > 0:
                raise ResolutionError(
                    "That link points to a website asset (CSS/JavaScript/font), not a downloadable file."
                )

            if self._is_torrent(final_url, content_type):
                return ResolvedLink(
                    url=final_url,
                    filename=filename_from_headers(response.headers) or filename_from_url(final_url, "file.torrent"),
                    referer=url if final_url != url else None,
                    content_type=content_type,
                    size=size,
                    kind="torrent",
                )

            if content_type in HTML_TYPES and not is_attachment:
                body = _read_limited(response, self.max_html_bytes)
                candidates, title = self._extract_candidates(body, final_url)
                if not candidates:
                    raise ResolutionError(
                        "That page does not expose a direct file. It may need a login, a CAPTCHA, "
                        "or a JavaScript-only download."
                    )

                for score, candidate in candidates[:10]:
                    probe = self._probe(candidate, referer=final_url)
                    if probe.link:
                        if not probe.link.filename or probe.link.filename == "download.bin":
                            probe.link.filename = _title_filename(title, probe.link.content_type)
                        return probe.link
                    if probe.html and depth < 2 and score >= 5:
                        try:
                            nested = self.resolve(candidate, depth + 1)
                        except (ResolutionError, InvalidUrl):
                            continue
                        if nested.referer is None:
                            nested.referer = final_url
                        return nested

                raise ResolutionError(
                    "Found download buttons on that page, but none pointed to a downloadable file."
                )

            filename = filename_from_headers(response.headers) or filename_from_url(final_url)
            return ResolvedLink(
                url=final_url,
                filename=filename,
                referer=url if final_url != url else None,
                content_type=content_type or None,
                size=size,
                kind="http",
                force_download=is_attachment,
            )
        finally:
            response.close()

    # --------------------------------------------------------------- internals
    def _is_torrent(self, url: str, content_type: str) -> bool:
        return content_type in TORRENT_TYPES or urlparse(url).path.lower().endswith(".torrent")

    def _special_url(self, url: str) -> str:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path = parsed.path
        query = parse_qs(parsed.query)

        if host == "drive.google.com":
            match = re.search(r"/file/d/([A-Za-z0-9_-]{10,})", path)
            file_id = match.group(1) if match else (query.get("id") or [None])[0]
            if not file_id:
                if "/drive/folders/" in path:
                    raise ResolutionError("Google Drive folders are not supported. Share a single file.")
                raise ResolutionError("Could not find a Google Drive file id in that link.")
            return f"https://drive.usercontent.google.com/download?id={quote(file_id)}&export=download&confirm=t"

        if host == "github.com":
            match = re.match(r"^/([^/]+)/([^/]+)/releases(?:/latest|/tag/([^/]+))?/?$", path)
            if match:
                owner, repo, tag = match.groups()
                api = (
                    f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{quote(tag)}"
                    if tag
                    else f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
                )
                try:
                    response = self.session.get(
                        api,
                        timeout=(10, 20),
                        headers={"Accept": "application/vnd.github+json"},
                    )
                    if response.status_code == 200:
                        release = response.json()
                        assets = release.get("assets") or []
                        if assets:
                            download = assets[0].get("browser_download_url")
                            if download:
                                return download
                        if release.get("zipball_url"):
                            return release["zipball_url"]
                except (requests.RequestException, ValueError):
                    pass

        if host == "mega.nz" or host == "mega.co.nz":
            raise ResolutionError(
                "MEGA links are end-to-end encrypted and cannot be mirrored by this bot. "
                "Use a direct link instead."
            )

        if host == "pixeldrain.com" and path.startswith("/u/"):
            file_id = path.split("/u/", 1)[1].split("/")[0]
            if file_id:
                return f"https://pixeldrain.com/api/file/{quote(file_id)}?download"

        if host in {"dropbox.com", "dropboxusercontent.com"}:
            query["dl"] = ["1"]
            parsed = parsed._replace(query="&".join(f"{k}={v[0]}" for k, v in query.items()))
            return urlunparse(parsed)

        return url

    # ------------------------------------------------------------------ Gofile
    def _resolve_gofile(self, code: str) -> ResolvedLink:
        try:
            return self._resolve_gofile_inner(code)
        except NoFallbackResolutionError:
            raise
        except _GofileAuthError as exc:
            raise NoFallbackResolutionError(
                "Gofile rejected the guest token. Try the link again in a few minutes."
            ) from exc
        except ResolutionError as exc:
            # Gofile failures are definitive; yt-dlp cannot read these links.
            raise NoFallbackResolutionError(str(exc)) from exc

    def _resolve_gofile_inner(self, code: str) -> ResolvedLink:
        token = self._gofile_account()
        try:
            data = self._gofile_fetch(code, token)
        except _GofileAuthError:
            token = self._gofile_account(force=True)
            try:
                data = self._gofile_fetch(code, token)
            except _GofileAuthError as exc:
                raise ResolutionError(
                    "Gofile rejected the guest token. Try the link again in a few minutes."
                ) from exc

        if not data.get("canAccess", True):
            if data.get("password") or data.get("passwordStatus"):
                raise ResolutionError(
                    "That Gofile folder is password-protected. The bot cannot open it."
                )
            raise ResolutionError(
                "That Gofile link is not accessible. It may have expired or been removed."
            )

        if data.get("type") == "file":
            return self._gofile_file_link(data, token)

        if data.get("type") == "folder":
            files = self._gofile_folder_files(code, token, first=data)
            if not files:
                raise ResolutionError("That Gofile folder is empty.")
            parts = [self._gofile_file_link(item, token) for item in files]
            if len(parts) == 1:
                return parts[0]
            parts.sort(key=lambda item: item.size or 0, reverse=True)
            largest = parts[0]
            return ResolvedLink(
                url=largest.url,
                filename=largest.filename,
                referer=largest.referer,
                content_type=largest.content_type,
                size=sum(item.size or 0 for item in parts),
                kind="http",
                headers=largest.headers,
                parts=parts,
                allow_ytdlp_fallback=False,
            )

        raise ResolutionError("That Gofile link does not point to a downloadable file.")

    def _gofile_account(self, force: bool = False) -> str:
        global _gofile_token
        if self.gofile_token and not force:
            return self.gofile_token
        with _gofile_token_lock:
            now = time.time()
            if not force and _gofile_token:
                token, created = _gofile_token
                if now - created < GOFILE_TOKEN_TTL:
                    return token

            try:
                response = self.session.post(
                    f"{GOFILE_API}/accounts",
                    timeout=self.timeout,
                    headers={"Accept": "application/json", "Referer": "https://gofile.io/"},
                )
            except requests.RequestException as exc:
                raise ResolutionError(f"Could not create a Gofile guest account: {exc}") from exc

            if response.status_code >= 400:
                raise ResolutionError(
                    f"Gofile refused the guest account request (HTTP {response.status_code})."
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise ResolutionError("Gofile returned an unreadable account response.") from exc

            token = (payload.get("data") or {}).get("token")
            if payload.get("status") != "ok" or not token:
                raise ResolutionError("Gofile did not provide a guest account token.")

            _gofile_token = (str(token), now)
            return str(token)

    def _gofile_headers(self, token: str) -> dict[str, str]:
        window = int(time.time()) // 14400
        raw = f"{USER_AGENT}::{GOFILE_LANG}::{token}::{window}::{GOFILE_WT_SALT}"
        website_token = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return {
            "Authorization": f"Bearer {token}",
            "X-Website-Token": website_token,
            "X-BL": GOFILE_LANG,
            "Accept": "application/json",
            "Referer": "https://gofile.io/",
        }

    def _gofile_fetch(self, code: str, token: str, page: int = 1) -> dict:
        url = f"{GOFILE_API}/contents/{quote(code, safe='')}"
        params = {
            "cache": "true",
            "sortField": "createTime",
            "sortDirection": "1",
            "page": str(page),
            "pageSize": "100",
        }
        try:
            response = self.session.get(
                url,
                params=params,
                headers=self._gofile_headers(token),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ResolutionError(f"Could not reach Gofile: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            if response.status_code == 401:
                raise _GofileAuthError() from exc
            raise ResolutionError("Gofile returned an unreadable response.") from exc

        status = str(payload.get("status") or "")
        status_lower = status.lower()
        if status == "ok":
            data = payload.get("data")
            if not isinstance(data, dict) or not data:
                raise ResolutionError("Gofile returned no content for that link.")
            return data
        if status_lower == "error-notpremium":
            raise ResolutionError("Gofile requires a premium account for that content.")
        if response.status_code == 401 or status_lower in {"error-token", "error-account"}:
            raise _GofileAuthError()
        if status_lower == "error-notfound":
            raise ResolutionError("That Gofile link does not exist or has expired.")
        if status_lower in {
            "error-password",
            "error-passwordrequired",
            "error-password-required",
        }:
            raise ResolutionError(
                "That Gofile folder is password-protected. The bot cannot open it."
            )
        if status_lower == "error-ratelimit":
            raise ResolutionError("Gofile is rate-limiting requests. Try again in a few minutes.")
        raise ResolutionError(f"Gofile returned an error: {status or 'unknown'}.")

    def _gofile_folder_files(
        self, code: str, token: str, first: Optional[dict] = None
    ) -> list[dict]:
        files: list[dict] = []
        queue: list[dict] = [first] if first else [self._gofile_fetch(code, token)]
        visited: set[str] = {code}
        seen_pages: set[tuple[str, int]] = set()
        while queue:
            folder = queue.pop(0)
            for child in (folder.get("children") or {}).values():
                if not isinstance(child, dict):
                    continue
                if child.get("type") == "file":
                    files.append(child)
                elif child.get("type") == "folder":
                    child_code = child.get("code") or child.get("id")
                    if not child_code or child_code in visited:
                        continue
                    visited.add(str(child_code))
                    try:
                        queue.append(self._gofile_fetch(str(child_code), token))
                    except ResolutionError:
                        logger.warning("Skipping inaccessible Gofile subfolder %s", child_code)

            metadata = folder.get("metadata") or {}
            if metadata.get("hasNextPage"):
                folder_code = str(folder.get("code") or code)
                try:
                    next_page = int(metadata.get("page") or 1) + 1
                    key = (folder_code, next_page)
                    if key not in seen_pages:
                        seen_pages.add(key)
                        queue.append(self._gofile_fetch(folder_code, token, next_page))
                except (ResolutionError, TypeError, ValueError):
                    pass
        return files

    def _gofile_file_link(self, data: dict, token: str) -> ResolvedLink:
        name = str(data.get("name") or data.get("code") or "file")
        link = data.get("link")
        if not link:
            file_id = data.get("id")
            servers = data.get("servers") or []
            server = data.get("serverSelected") or next(
                (item for item in servers if item), None
            )
            if file_id and server:
                link = (
                    f"https://{server}.gofile.io/download/web/"
                    f"{quote(str(file_id), safe='')}/{quote(name, safe='')}"
                )
        if not link:
            raise ResolutionError(f"Gofile did not provide a download link for {name}.")

        size = data.get("size")
        try:
            size_value = int(size) if size is not None else None
        except (TypeError, ValueError):
            size_value = None
        mimetype = data.get("mimetype")
        return ResolvedLink(
            url=str(link),
            filename=sanitize_filename(name, "file.bin"),
            referer="https://gofile.io/",
            content_type=(str(mimetype).split(";")[0].strip() if mimetype else None),
            size=size_value if size_value and size_value > 0 else None,
            kind="http",
            headers={"Authorization": f"Bearer {token}"},
            allow_ytdlp_fallback=False,
        )

    def _probe(self, url: str, referer: str) -> _Probe:
        headers = {"Referer": referer}
        try:
            response = self.session.head(
                url,
                allow_redirects=True,
                timeout=(10, 20),
                headers=headers,
            )
            status = response.status_code
            content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            head_disposition = (response.headers.get("content-disposition") or "").lower()

            # Some servers reject HEAD, and some answer HEAD with HTML while GET works.
            if status in {403, 405, 501} or (
                status < 400
                and content_type in HTML_TYPES
                and "attachment" not in head_disposition
            ):
                response.close()
                response = self.session.get(
                    url,
                    stream=True,
                    allow_redirects=True,
                    timeout=(10, 25),
                    headers={**headers, "Range": "bytes=0-0"},
                )
                status = response.status_code
                content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()

            try:
                if status >= 400:
                    return _Probe()
                final_url = response.url
                disposition = (response.headers.get("content-disposition") or "").lower()
                is_attachment = "attachment" in disposition
                if content_type in HTML_TYPES and not is_attachment:
                    return _Probe(html=True)
                if self._is_torrent(final_url, content_type):
                    return _Probe(
                        link=ResolvedLink(
                            url=final_url,
                            filename=filename_from_headers(response.headers) or filename_from_url(final_url, "file.torrent"),
                            referer=referer,
                            content_type=content_type,
                            size=_content_length(response.headers),
                            kind="torrent",
                        )
                    )
                return _Probe(
                    link=ResolvedLink(
                        url=final_url,
                        filename=filename_from_headers(response.headers) or filename_from_url(final_url),
                        referer=referer,
                        content_type=content_type or None,
                        size=_content_length(response.headers),
                        kind="http",
                        force_download=is_attachment,
                    )
                )
            finally:
                response.close()
        except requests.RequestException:
            return _Probe()

    def _extract_candidates(self, body: bytes, base_url: str) -> tuple[list[tuple[int, str]], str]:
        text = body.decode("utf-8", errors="replace")
        soup = BeautifulSoup(body, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        found: dict[str, int] = {}

        def add(raw: Optional[str], score: int) -> None:
            if not raw:
                return
            raw = html_lib.unescape(str(raw).strip().strip("'\""))
            if not raw or BAD_HREF.match(raw):
                return
            absolute = urljoin(base_url, raw)
            if not absolute.lower().startswith(("http://", "https://")):
                return
            if absolute == base_url:
                return
            if ASSET_EXT.search(urlparse(absolute).path):
                score -= 8
            if score <= 0:
                return
            found[absolute] = max(found.get(absolute, -100), score)

        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            label = " ".join(anchor.get_text(" ", strip=True).split())
            hints = " ".join(
                [
                    label,
                    str(anchor.get("aria-label") or ""),
                    str(anchor.get("title") or ""),
                    " ".join(anchor.get("class") or []),
                    str(anchor.get("id") or ""),
                ]
            )
            score = 0
            if anchor.has_attr("download"):
                score += 8
            if DOWNLOAD_WORDS.search(hints):
                score += 6
            if FILE_EXT.search(urlparse(href).path):
                score += 4
            if re.search(r"/(download|dl|get|file)(/|$)", href, re.IGNORECASE):
                score += 3
            if re.search(r"/(releases/download|download/file|download\.php)", href, re.IGNORECASE):
                score += 4
            if score:
                add(href, score)

        # data-* attributes on buttons/links, e.g. data-url / data-download-url
        for element in soup.find_all(True):
            for key, value in element.attrs.items():
                if not key.startswith("data-") or not isinstance(value, str):
                    continue
                if not re.search(r"(url|href|download|file|src)", key, re.IGNORECASE):
                    continue
                score = 5 if "download" in key.lower() else 3
                add(value, score)
            onclick = element.get("onclick")
            if isinstance(onclick, str):
                for match in re.finditer(r"https?://[^'\"\s)]+", onclick):
                    add(match.group(0), 4)
                for match in re.finditer(
                    r"(?:location(?:\.href)?|location\.assign|window\.open)\s*(?:=|\()\s*['\"]([^'\"]+)['\"]",
                    onclick,
                    re.IGNORECASE,
                ):
                    add(match.group(1), 4)

        for tag, attr in (
            ("video", "src"),
            ("audio", "src"),
            ("source", "src"),
            ("embed", "src"),
            ("iframe", "src"),
        ):
            for element in soup.find_all(tag):
                add(element.get(attr), 6 if tag in {"video", "audio", "source"} else 2)

        for meta in soup.find_all("meta"):
            prop = (meta.get("property") or meta.get("name") or "").lower()
            if prop in {
                "og:video",
                "og:video:url",
                "og:video:secure_url",
                "og:audio",
                "og:audio:url",
                "twitter:player:stream",
            }:
                add(meta.get("content"), 5)
            if "refresh" in prop or "refresh" in str(meta.get("http-equiv") or "").lower():
                match = re.search(r"url\s*=\s*([^;]+)", str(meta.get("content") or ""), re.IGNORECASE)
                if match:
                    add(match.group(1).strip(" '\""), 5)

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except (ValueError, TypeError):
                continue
            for value in _walk_json(data):
                add(value, 5)

        for pattern in (
            r"(?:window\.)?location(?:\.href)?\s*=\s*['\"]([^'\"]+)['\"]",
            r"location\.assign\(\s*['\"]([^'\"]+)['\"]",
            r"window\.open\(\s*['\"]([^'\"]+)['\"]",
        ):
            for match in re.finditer(pattern, text, re.IGNORECASE):
                add(match.group(1), 3)

        candidates = sorted(
            ((link, score) for link, score in found.items() if score > 0),
            key=lambda item: item[1],
            reverse=True,
        )
        return [(score, link) for link, score in candidates], title


def _gofile_code(url: str) -> Optional[str]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host not in {"gofile.io", "gofile.com"}:
        return None

    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) >= 2 and segments[0].lower() in {"d", "download"}:
        return segments[1]

    query = parse_qs(parsed.query)
    return (query.get("c") or query.get("code") or [None])[0]


def _walk_json(value) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"contentUrl", "embedUrl", "url"} and isinstance(item, str):
                yield item
            else:
                yield from _walk_json(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json(item)


def _content_length(headers) -> Optional[int]:
    try:
        length = int(headers.get("content-length", ""))
        return length if length > 0 else None
    except (TypeError, ValueError):
        return None


def _read_limited(response: requests.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(64 * 1024):
        if not chunk:
            continue
        remaining = limit - total
        if remaining <= 0:
            break
        chunks.append(chunk[:remaining])
        total += min(len(chunk), remaining)
    return b"".join(chunks)


def _title_filename(title: str, content_type: Optional[str]) -> Optional[str]:
    if not title:
        return None
    extension = ""
    if content_type:
        if "pdf" in content_type:
            extension = ".pdf"
        elif "zip" in content_type:
            extension = ".zip"
        elif "video/mp4" in content_type:
            extension = ".mp4"
    return sanitize_filename(title + extension, "download.bin")
