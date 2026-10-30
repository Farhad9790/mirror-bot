"""Send downloaded files directly to the requesting Telegram chat.

Telegram's cloud Bot API allows bots to upload files up to 50 MB. Files above
the configured safe limit are split into numbered parts and sent one at a
time. Parts are created and deleted sequentially, so a large file only needs
space for the source plus one part instead of a full second copy.
"""

from __future__ import annotations

import logging
import math
import shutil
import tempfile
import time
from pathlib import Path
from typing import Iterator, Optional

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

from ..errors import JobCancelled, UploadError, UploaderUnavailable
from ..utils import CancelToken, human_size, sanitize_filename
from .base import ProgressCB, Uploader, UploadResult

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
CHUNK_SIZE = 1024 * 1024


class _Retryable(Exception):
    def __init__(self, message: str, wait: Optional[float] = None) -> None:
        super().__init__(message)
        self.wait = wait


class TelegramUploader(Uploader):
    name = "telegram"

    def __init__(
        self,
        token: Optional[str],
        chat_id: Optional[int] = None,
        *,
        max_upload: int = 45 * 1024**2,
        part_size: int = 45 * 1024**2,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.max_upload = max(1, max_upload)
        self.part_size = max(1, min(part_size, self.max_upload))
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "mirror-bot/1.0"})

    def available(self) -> tuple[bool, str]:
        if not self.token:
            return False, "Telegram is not configured (missing bot token)."
        return True, ""

    def upload(self, path: Path, progress: ProgressCB, cancel: CancelToken) -> UploadResult:
        ok, reason = self.available()
        if not ok:
            raise UploaderUnavailable(reason)
        if self.chat_id is None:
            raise UploadError("No Telegram chat is associated with this job.")
        if not path.is_file():
            raise UploadError("The downloaded file disappeared before upload.")

        size = path.stat().st_size
        if size == 0:
            raise UploadError("Refusing to send an empty file.")

        if size <= self.max_upload:
            progress(0, "Sending to Telegram…")
            self._send_file(
                path=path,
                filename=sanitize_filename(path.name, "file"),
                caption=f"📄 {path.name}",
                progress=progress,
                cancel=cancel,
                total_size=size,
                sent_before=0,
                label="",
            )
            return UploadResult(url="", host="Telegram")

        total_parts = math.ceil(size / self.part_size)
        progress(0, f"Splitting into {total_parts} parts…")
        parts_dir = Path(
            tempfile.mkdtemp(prefix=".telegram-parts-", dir=str(path.parent))
        )
        sent_before = 0
        try:
            for index, part_path in _iter_parts(path, self.part_size, cancel, parts_dir):
                cancel.check()
                if index == 1:
                    caption = (
                        f"📦 {path.name} — part 1/{total_parts}\n"
                        "Download every part, then rejoin them:\n"
                        f"Windows: copy /b \"{path.name}.part*\" \"{path.name}\"\n"
                        f"Linux/macOS: cat \"{path.name}.part\"* > \"{path.name}\""
                    )
                else:
                    caption = f"📦 {path.name} — part {index}/{total_parts}"

                part_bytes = part_path.stat().st_size
                self._send_file(
                    path=part_path,
                    filename=sanitize_filename(part_path.name, "part"),
                    caption=caption,
                    progress=progress,
                    cancel=cancel,
                    total_size=size,
                    sent_before=sent_before,
                    label=f"Part {index}/{total_parts} · ",
                )
                sent_before += part_bytes
                part_path.unlink(missing_ok=True)
                if index < total_parts:
                    _sleep(1.5, cancel)
        finally:
            shutil.rmtree(parts_dir, ignore_errors=True)

        progress(100, f"Sent {total_parts} parts to this chat.")
        return UploadResult(
            url="",
            host="Telegram",
            detail=f"Sent as {total_parts} parts.",
        )

    # ------------------------------------------------------------------ HTTP
    def _send_file(
        self,
        *,
        path: Path,
        filename: str,
        caption: str,
        progress: ProgressCB,
        cancel: CancelToken,
        total_size: int,
        sent_before: int,
        label: str,
    ) -> None:
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                self._send_once(
                    path=path,
                    filename=filename,
                    caption=caption,
                    progress=progress,
                    cancel=cancel,
                    total_size=total_size,
                    sent_before=sent_before,
                    label=label,
                )
                return
            except (JobCancelled, UploadError):
                raise
            except _Retryable as exc:
                last_error = exc
                if attempt == 2:
                    break
                _sleep(exc.wait if exc.wait is not None else 2**attempt, cancel)
            except requests.RequestException as exc:
                last_error = exc
                if attempt == 2:
                    break
                _sleep(2**attempt, cancel)
        raise UploadError(f"Telegram upload failed: {last_error}")

    def _send_once(
        self,
        *,
        path: Path,
        filename: str,
        caption: str,
        progress: ProgressCB,
        cancel: CancelToken,
        total_size: int,
        sent_before: int,
        label: str,
    ) -> None:
        with path.open("rb") as handle:
            fields = {
                "chat_id": str(self.chat_id),
                "caption": caption,
                "document": (filename, handle, "application/octet-stream"),
            }
            encoder = MultipartEncoder(fields=fields)
            part_bytes = path.stat().st_size
            request_bytes = encoder.len or 1

            def on_progress(monitor: MultipartEncoderMonitor) -> None:
                cancel.check()
                # Multipart overhead (boundaries and form fields) should not be
                # shown as file bytes. Scale the request progress to the file.
                sent_in_part = min(
                    part_bytes,
                    int(monitor.bytes_read / request_bytes * part_bytes),
                )
                sent_total = sent_before + sent_in_part
                percent = None
                if total_size:
                    percent = min(100.0, sent_total / total_size * 100)
                detail = (
                    f"{label}{human_size(sent_total)}"
                    f" / {human_size(total_size)}"
                )
                progress(percent, detail)

            monitor = MultipartEncoderMonitor(encoder, on_progress)
            response = self.session.post(
                f"{API_BASE}/bot{self.token}/sendDocument",
                data=monitor,
                headers={"Content-Type": monitor.content_type},
                timeout=(30, 600),
            )

        if response.status_code == 429:
            wait = 5
            try:
                payload = response.json()
                wait = int((payload.get("parameters") or {}).get("retry_after") or 5)
            except (TypeError, ValueError):
                pass
            raise _Retryable(
                "Telegram rate limit hit.",
                wait=min(60, max(1, wait)),
            )
        if response.status_code in {500, 502, 503, 504}:
            raise _Retryable(f"Telegram returned HTTP {response.status_code}")
        if response.status_code >= 400:
            message = response.text[:300]
            try:
                payload = response.json()
                message = payload.get("description") or message
            except ValueError:
                pass
            raise UploadError(f"Telegram rejected the file: {message}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise UploadError("Telegram returned an unexpected response.") from exc

        if not payload.get("ok"):
            description = payload.get("description") or "unknown error"
            raise UploadError(f"Telegram upload failed: {description}")

        logger.info("Telegram file sent: %s", filename)


def _iter_parts(
    path: Path,
    part_size: int,
    cancel: CancelToken,
    parts_dir: Path,
) -> Iterator[tuple[int, Path]]:
    """Yield numbered part files, reading the source from the beginning."""
    total_parts = math.ceil(path.stat().st_size / part_size)
    base = sanitize_filename(path.name, "file")

    with path.open("rb") as source:
        for index in range(1, total_parts + 1):
            cancel.check()
            part_path = parts_dir / f"{base}.part{index:03d}"
            remaining = part_size
            with part_path.open("wb") as target:
                while remaining > 0:
                    chunk = source.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    target.write(chunk)
                    remaining -= len(chunk)
                    cancel.check()

            if part_path.stat().st_size == 0:
                part_path.unlink(missing_ok=True)
                break
            yield index, part_path


def _sleep(seconds: float, cancel: CancelToken) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        cancel.check()
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
