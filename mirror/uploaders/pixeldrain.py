"""Pixeldrain uploader. Requires an API key (anonymous uploads are disabled)."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

from ..errors import JobCancelled, UploadError, UploaderUnavailable
from ..utils import CancelToken, human_size
from .base import ProgressCB, Uploader, UploadResult

logger = logging.getLogger(__name__)

API_BASE = "https://pixeldrain.com/api"


class _ProgressReader:
    """File wrapper that reports upload progress to requests."""

    def __init__(
        self,
        handle,
        total: int,
        progress: ProgressCB,
        cancel: CancelToken,
    ) -> None:
        self.handle = handle
        self.total = total
        self.progress = progress
        self.cancel = cancel
        self.read_bytes = 0
        self._last_report_bytes = 0
        self._last_report_time = time.monotonic()

    def __len__(self) -> int:
        return self.total

    def read(self, size: int = -1) -> bytes:
        self.cancel.check()
        chunk = self.handle.read(size)
        if chunk:
            self.read_bytes += len(chunk)
            now = time.monotonic()
            if (
                self.read_bytes - self._last_report_bytes >= 1024 * 1024
                or now - self._last_report_time >= 1.0
                or self.read_bytes >= self.total
            ):
                percent = self.read_bytes / self.total * 100 if self.total else None
                self.progress(percent, f"{human_size(self.read_bytes)} / {human_size(self.total)}")
                self._last_report_bytes = self.read_bytes
                self._last_report_time = now
        return chunk


class PixeldrainUploader(Uploader):
    name = "pixeldrain"

    def __init__(self, api_key: Optional[str], session: Optional[requests.Session] = None) -> None:
        self.api_key = api_key
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "mirror-bot/1.0"})

    def available(self) -> tuple[bool, str]:
        if not self.api_key:
            return (
                False,
                (
                    "Pixeldrain needs an API key. Add PIXELDRAIN_API_KEY to the bot's secrets "
                    "(pixeldrain.com → Settings → API keys)."
                ),
            )
        return True, ""

    def upload(self, path: Path, progress: ProgressCB, cancel: CancelToken) -> UploadResult:
        ok, reason = self.available()
        if not ok:
            raise UploaderUnavailable(reason)
        if not path.is_file():
            raise UploadError("The downloaded file disappeared before upload.")

        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                return self._upload_once(path, progress, cancel)
            except (JobCancelled, UploadError):
                raise
            except _Retryable as exc:
                last_error = exc
                if attempt == 2:
                    break
                _sleep(exc.wait if exc.wait is not None else 2 ** attempt, cancel)
            except requests.RequestException as exc:
                last_error = exc
                if attempt == 2:
                    break
                _sleep(2 ** attempt, cancel)
        raise UploadError(f"Pixeldrain upload failed: {last_error}")

    def _upload_once(self, path: Path, progress: ProgressCB, cancel: CancelToken) -> UploadResult:
        size = path.stat().st_size
        if size == 0:
            raise UploadError("Refusing to upload an empty file.")
        progress(0, "Contacting Pixeldrain…")

        url = f"{API_BASE}/file/{quote(path.name, safe='')}"
        with path.open("rb") as handle:
            reader = _ProgressReader(handle, size, progress, cancel)
            response = self.session.put(
                url,
                data=reader,
                headers={
                    "Content-Length": str(size),
                    "Content-Type": "application/octet-stream",
                },
                auth=("", self.api_key),
                timeout=(30, None),
            )

        if response.status_code == 429:
            retry_after = response.headers.get("retry-after", "5")
            try:
                wait = min(60, max(1, int(retry_after)))
            except ValueError:
                wait = 5
            raise _Retryable(f"Pixeldrain rate limit hit; retry after {wait}s", wait=wait)
        if response.status_code in {500, 502, 503, 504}:
            raise _Retryable(f"Pixeldrain returned HTTP {response.status_code}")
        if response.status_code == 401:
            raise UploadError("Pixeldrain rejected the API key. Check PIXELDRAIN_API_KEY.")
        if response.status_code == 413:
            raise UploadError("The file is too large for your Pixeldrain plan.")
        if response.status_code >= 400:
            raise UploadError(f"Pixeldrain returned HTTP {response.status_code}: {response.text[:200]}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise UploadError("Pixeldrain returned an unexpected response.") from exc

        if not payload.get("success"):
            message = payload.get("message") or payload.get("value") or "unknown error"
            raise UploadError(f"Pixeldrain upload failed: {message}")

        file_id = payload.get("id")
        if not file_id:
            raise UploadError("Pixeldrain did not return a file id.")

        link = f"https://pixeldrain.com/u/{file_id}"
        logger.info("Pixeldrain upload complete: %s", link)
        return UploadResult(url=link, host="Pixeldrain")


class _Retryable(Exception):
    def __init__(self, message: str, wait: Optional[float] = None) -> None:
        super().__init__(message)
        self.wait = wait


def _sleep(seconds: float, cancel: CancelToken) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        cancel.check()
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
