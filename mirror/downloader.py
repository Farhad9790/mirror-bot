"""Download engines: aria2c for HTTP/torrent, yt-dlp for video sites."""

from __future__ import annotations

import importlib.util
import logging
import re
import shutil
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import requests

from .config import Settings
from .errors import (
    DownloadError,
    FileTooLarge,
    JobCancelled,
    JobTimeout,
    MirrorError,
)
from .resolver import ResolvedLink, USER_AGENT
from .utils import (
    CancelToken,
    filename_from_headers,
    filename_from_url,
    human_size,
    human_speed,
    parse_aria_size,
    sanitize_filename,
)

logger = logging.getLogger(__name__)

ProgressCB = Callable[[Optional[float], str], None]

READOUT_RE = re.compile(
    r"\[#\w+\s+([\d.]+\s*[KMGT]?i?B)/([\d.]+\s*[KMGT]?i?B)"
    r"(?:\((\d+)%\))?(?:.*?\bDL:([\d.]+\s*[KMGT]?i?B))?",
    re.IGNORECASE,
)


@dataclass(slots=True)
class DownloadResult:
    path: Path
    size: int
    extras: int = 0


class Downloader:
    def __init__(self, settings: Settings, cancel: CancelToken) -> None:
        self.settings = settings
        self.cancel = cancel
        self.max_size = settings.max_file_size

    # ------------------------------------------------------------------ public
    def download(
        self,
        *,
        original_url: str,
        link: ResolvedLink,
        dest: Path,
        progress: ProgressCB,
    ) -> DownloadResult:
        dest.mkdir(parents=True, exist_ok=True)

        if link.kind == "magnet":
            return self._torrent(link.url, dest, progress)
        if link.kind == "torrent":
            return self._torrent(link.url, dest, progress, referer=link.referer)
        if link.kind == "ytdlp":
            return self._ytdlp(original_url, dest, progress)

        if link.size and link.size > self.max_size:
            raise FileTooLarge(
                f"The file is {human_size(link.size)}, above the {human_size(self.max_size)} limit."
            )

        try:
            return self._http(link, dest, progress)
        except (JobCancelled, JobTimeout, FileTooLarge):
            raise
        except DownloadError as exc:
            if not self._ytdlp_available():
                raise
            progress(None, "Direct download failed, trying video extractor…")
            try:
                return self._ytdlp(original_url, dest, progress)
            except (JobCancelled, JobTimeout):
                raise
            except MirrorError:
                raise exc

    # -------------------------------------------------------------- HTTP/aria2
    def _http(self, link: ResolvedLink, dest: Path, progress: ProgressCB) -> DownloadResult:
        if shutil.which(self.settings.aria2c_path):
            return self._aria2_http(link, dest, progress)
        logger.warning("%s not found; falling back to the Python downloader", self.settings.aria2c_path)
        return self._requests_download(link, dest, progress)

    def _aria2_http(self, link: ResolvedLink, dest: Path, progress: ProgressCB) -> DownloadResult:
        out_name = sanitize_filename(link.filename or filename_from_url(link.url), "download.bin")
        args = [
            "--dir", str(dest),
            "--out", out_name,
            "--continue=true",
            "--allow-overwrite=true",
            "--auto-file-renaming=false",
            "--file-allocation=none",
            "--max-connection-per-server=16",
            "--split=16",
            "--min-split-size=1M",
            "--max-tries=5",
            "--retry-wait=5",
            "--connect-timeout=30",
            "--timeout=60",
            "--summary-interval=3",
            "--console-log-level=warn",
            "--download-result=hide",
            f"--user-agent={USER_AGENT}",
        ]
        if link.referer:
            args.append(f"--referer={link.referer}")
        args.append(link.url)

        self._run_aria2(args, dest, progress, label="download")
        result = self._collect(dest)
        if result.size > self.max_size:
            raise FileTooLarge(
                f"The file is {human_size(result.size)}, above the {human_size(self.max_size)} limit."
            )
        return result

    def _torrent(
        self,
        url: str,
        dest: Path,
        progress: ProgressCB,
        *,
        referer: Optional[str] = None,
    ) -> DownloadResult:
        if not shutil.which(self.settings.aria2c_path):
            raise DownloadError(
                "Torrent and magnet links need aria2c. Install aria2 (it is preinstalled in the "
                "Docker image and GitHub Actions workflow)."
            )

        progress(None, "Fetching torrent metadata…")
        args = [
            "--dir", str(dest),
            "--seed-time=0",
            "--bt-stop-timeout=300",
            "--bt-save-metadata=false",
            "--follow-torrent=mem",
            "--bt-tracker-connect-timeout=30",
            "--bt-tracker-timeout=30",
            "--max-concurrent-downloads=1",
            "--file-allocation=none",
            "--summary-interval=3",
            "--console-log-level=warn",
            "--download-result=hide",
            "--max-tries=5",
            "--retry-wait=5",
            "--connect-timeout=30",
            "--timeout=60",
            "--user-agent=" + USER_AGENT,
        ]
        if referer:
            args.append(f"--referer={referer}")
        args.append(url)
        self._run_aria2(args, dest, progress, label="torrent")
        result = self._collect(dest)
        if result.size > self.max_size:
            raise FileTooLarge(
                f"The torrent's largest file is {human_size(result.size)}, above the "
                f"{human_size(self.max_size)} limit."
            )
        return result

    def _run_aria2(
        self,
        args: list[str],
        dest: Path,
        progress: ProgressCB,
        *,
        label: str,
    ) -> None:
        command = [self.settings.aria2c_path, *args]
        logger.info("Starting aria2 %s", label)
        try:
            process = subprocess.Popen(
                command,
                cwd=str(dest),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise DownloadError(f"Could not start aria2c: {exc}") from exc

        tail: deque[str] = deque(maxlen=25)
        try:
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                tail.append(line)
                self.cancel.check()
                match = READOUT_RE.search(line)
                if not match:
                    continue
                downloaded = match.group(1)
                total = match.group(2)
                percent = float(match.group(3)) if match.group(3) else None
                speed = match.group(4) or ""

                total_bytes = parse_aria_size(total)
                if total_bytes and total_bytes > self.max_size:
                    process.terminate()
                    raise FileTooLarge(
                        f"The file is {human_size(total_bytes)}, above the "
                        f"{human_size(self.max_size)} limit."
                    )

                if total_bytes == 0:
                    progress(
                        None,
                        "Fetching torrent metadata…" if label == "torrent" else "Waiting for server…",
                    )
                    continue
                detail = f"{downloaded} / {total}"
                if speed and not speed.startswith("0"):
                    detail += f" · {speed}/s"
                progress(percent, detail)

            code = process.wait(timeout=60)
        except (JobCancelled, JobTimeout, FileTooLarge):
            _terminate(process)
            raise
        except subprocess.TimeoutExpired:
            _terminate(process)
            raise DownloadError("aria2c did not exit after finishing the transfer.")
        finally:
            if process.poll() is None:
                _terminate(process)

        if code != 0:
            if self.cancel.cancelled:
                raise JobCancelled("Cancelled.")
            details = [line for line in tail if not line.startswith("[#")]
            message = " | ".join(details[-4:]) or "no details from aria2c"
            raise DownloadError(f"aria2c failed with exit code {code}: {message}")

    def _requests_download(self, link: ResolvedLink, dest: Path, progress: ProgressCB) -> DownloadResult:
        name = sanitize_filename(link.filename or filename_from_url(link.url), "download.bin")
        target = dest / name
        partial = target.with_suffix(target.suffix + ".part")
        headers = {"User-Agent": USER_AGENT}
        if link.referer:
            headers["Referer"] = link.referer

        try:
            response = requests.get(
                link.url,
                headers=headers,
                stream=True,
                allow_redirects=True,
                timeout=(30, 60),
            )
        except requests.RequestException as exc:
            raise DownloadError(f"Download failed: {exc}") from exc

        try:
            response.raise_for_status()
            content_type = (response.headers.get("content-type") or "").lower()
            if "text/html" in content_type:
                raise DownloadError("The server returned a web page instead of a file.")
            remote_name = filename_from_headers(response.headers)
            if remote_name and (link.filename is None or link.filename == "download.bin"):
                target = dest / remote_name
                partial = target.with_suffix(target.suffix + ".part")

            total = None
            try:
                total = int(response.headers.get("content-length", ""))
            except (TypeError, ValueError):
                pass
            if total and total > self.max_size:
                raise FileTooLarge(
                    f"The file is {human_size(total)}, above the {human_size(self.max_size)} limit."
                )

            downloaded = 0
            started = time.monotonic()
            with open(partial, "wb") as handle:
                for chunk in response.iter_content(256 * 1024):
                    if not chunk:
                        continue
                    self.cancel.check()
                    downloaded += len(chunk)
                    if downloaded > self.max_size:
                        raise FileTooLarge(
                            f"The file grew past the {human_size(self.max_size)} limit."
                        )
                    handle.write(chunk)
                    elapsed = max(0.001, time.monotonic() - started)
                    speed = downloaded / elapsed
                    percent = downloaded / total * 100 if total else None
                    detail = f"{human_size(downloaded)} / {human_size(total)} · {human_speed(speed)}"
                    progress(percent, detail)
        except (JobCancelled, JobTimeout, FileTooLarge, DownloadError):
            partial.unlink(missing_ok=True)
            raise
        except requests.RequestException as exc:
            partial.unlink(missing_ok=True)
            raise DownloadError(f"Download failed: {exc}") from exc
        finally:
            response.close()

        if not partial.exists() or partial.stat().st_size == 0:
            raise DownloadError("The download was empty.")
        partial.replace(target)
        return DownloadResult(path=target, size=target.stat().st_size)

    # ------------------------------------------------------------------- yt-dlp
    def _ytdlp_available(self) -> bool:
        return importlib.util.find_spec("yt_dlp") is not None

    def _ytdlp(self, url: str, dest: Path, progress: ProgressCB) -> DownloadResult:
        if not self._ytdlp_available():
            raise DownloadError("yt-dlp is not installed, so this video site cannot be downloaded.")
        import yt_dlp  # imported lazily; the package is optional at runtime

        def hook(data: dict) -> None:
            self.cancel.check()
            status = data.get("status")
            if status == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                downloaded = data.get("downloaded_bytes") or 0
                percent = downloaded / total * 100 if total else None
                speed = data.get("speed")
                eta = data.get("eta")
                detail = f"{human_size(downloaded)} / {human_size(total)}"
                if speed:
                    detail += f" · {human_speed(speed)}"
                if eta:
                    detail += f" · ETA {int(eta)}s"
                progress(percent, detail)
            elif status == "finished":
                progress(100, "Processing video…")

        options = {
            "paths": {"home": str(dest), "temp": str(dest)},
            "outtmpl": "%(title).150B [%(id)s].%(ext)s",
            "format": self.settings.ytdlp_format,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "retries": 5,
            "fragment_retries": 5,
            "socket_timeout": 30,
            "concurrent_fragment_downloads": 4,
            "windowsfilenames": True,
            "overwrites": True,
            "max_filesize": self.max_size,
            "progress_hooks": [hook],
        }
        if self.settings.cookies_file and self.settings.cookies_file.exists():
            options["cookiefile"] = str(self.settings.cookies_file)

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.extract_info(url, download=True)
        except (JobCancelled, JobTimeout):
            raise
        except Exception as exc:  # yt-dlp raises many error classes
            message = str(exc).strip() or exc.__class__.__name__
            if "max-filesize" in message.lower() or "larger than max" in message.lower():
                raise FileTooLarge(
                    f"The video is larger than the {human_size(self.max_size)} limit."
                ) from exc
            raise DownloadError(f"Video extractor failed: {message[:400]}") from exc

        result = self._collect(dest)
        if result.size > self.max_size:
            raise FileTooLarge(
                f"The video is {human_size(result.size)}, above the {human_size(self.max_size)} limit."
            )
        return result

    # ------------------------------------------------------------------ output
    def _collect(self, dest: Path) -> DownloadResult:
        ignored = {".aria2", ".part", ".ytdl", ".tmp"}
        files = [
            path
            for path in dest.rglob("*")
            if path.is_file() and path.suffix.lower() not in ignored and path.stat().st_size > 0
        ]
        if not files:
            raise DownloadError("The download finished but produced no file.")

        files.sort(key=lambda path: path.stat().st_size, reverse=True)
        largest = files[0]
        extras = len(files) - 1

        if extras and self.settings.multi_file_mode == "zip":
            archive_base = dest.parent / sanitize_filename(dest.name, "download")
            archive = Path(shutil.make_archive(str(archive_base), "zip", root_dir=dest))
            shutil.rmtree(dest, ignore_errors=True)
            return DownloadResult(path=archive, size=archive.stat().st_size)

        if extras:
            logger.info("Picked the largest of %d downloaded files", len(files))
        return DownloadResult(path=largest, size=largest.stat().st_size, extras=extras)


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        try:
            process.kill()
        except OSError:
            pass
