"""Gofile uploader (new upload fleet API, guest uploads supported)."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

from ..errors import JobCancelled, UploadError
from ..utils import CancelToken, human_size
from .base import ProgressCB, Uploader, UploadResult

logger = logging.getLogger(__name__)

UPLOAD_URL = "https://upload.gofile.io/uploadfile"


class _Retryable(Exception):
    pass


class GofileUploader(Uploader):
    name = "gofile"

    def __init__(self, token: Optional[str] = None, session: Optional[requests.Session] = None) -> None:
        self.token = token
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "mirror-bot/1.0"})

    def available(self) -> tuple[bool, str]:
        return True, ""

    def upload(self, path: Path, progress: ProgressCB, cancel: CancelToken) -> UploadResult:
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
                _sleep(2 ** attempt, cancel)
            except requests.RequestException as exc:
                last_error = exc
                if attempt == 2:
                    break
                _sleep(2 ** attempt, cancel)
        raise UploadError(f"Gofile upload failed: {last_error}")

    def _upload_once(self, path: Path, progress: ProgressCB, cancel: CancelToken) -> UploadResult:
        progress(0, "Contacting Gofile…")
        with path.open("rb") as handle:
            fields = {"file": (path.name, handle, "application/octet-stream")}
            encoder = MultipartEncoder(fields=fields)
            total = encoder.len

            def on_progress(monitor: MultipartEncoderMonitor) -> None:
                cancel.check()
                percent = monitor.bytes_read / total * 100 if total else None
                progress(percent, f"{human_size(monitor.bytes_read)} / {human_size(total)}")

            monitor = MultipartEncoderMonitor(encoder, on_progress)
            headers = {"Content-Type": monitor.content_type}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"

            response = self.session.post(
                UPLOAD_URL,
                data=monitor,
                headers=headers,
                timeout=(30, None),
            )

        if response.status_code in {429, 500, 502, 503, 504}:
            raise _Retryable(f"Gofile returned HTTP {response.status_code}")
        if response.status_code in {401, 403}:
            raise UploadError(
                "Gofile rejected the account token. Check GOFILE_TOKEN or remove it to use guest uploads."
            )
        if response.status_code >= 400:
            raise UploadError(f"Gofile returned HTTP {response.status_code}: {response.text[:200]}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise UploadError("Gofile returned an unexpected response.") from exc

        if str(payload.get("status", "")).lower() != "ok":
            message = payload.get("message") or payload.get("error") or "unknown error"
            raise UploadError(f"Gofile upload failed: {message}")

        data = payload.get("data") or {}
        url = data.get("downloadPage") or data.get("link")
        if not url:
            code = data.get("code") or data.get("parentFolderCode")
            if code:
                url = f"https://gofile.io/d/{code}"
        if not url:
            raise UploadError("Gofile did not return a download link.")

        logger.info("Gofile upload complete: %s", url)
        return UploadResult(url=str(url), host="Gofile")


def _sleep(seconds: float, cancel: CancelToken) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        cancel.check()
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
