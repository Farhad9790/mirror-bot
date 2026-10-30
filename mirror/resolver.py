"""Turn a share/page URL into a direct downloadable link."""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import parse_qs, quote, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from .errors import InvalidUrl, ResolutionError
from .utils import (
    filename_from_headers,
    filename_from_url,
    is_magnet,
    normalize_url,
    sanitize_filename,
    validate_http_url,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

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
        session: Optional[requests.Session] = None,
    ) -> None:
        self.timeout = timeout
        self.max_html_bytes = max_html_bytes
        self.allow_private_urls = allow_private_urls
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

            if content_type in WEB_ASSET_TYPES:
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

            if content_type in HTML_TYPES:
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

        if host == "gofile.io" and path.startswith("/d/"):
            raise ResolutionError(
                "Gofile download pages cannot be read without a premium API token. "
                "Send the original source link instead."
            )

        return url

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

            # Some servers reject HEAD, and some answer HEAD with HTML while GET works.
            if status in {403, 405, 501} or (status < 400 and content_type in HTML_TYPES):
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
                if content_type in HTML_TYPES:
                    return _Probe(html=True)
                if content_type in WEB_ASSET_TYPES:
                    return _Probe()
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
