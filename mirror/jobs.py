"""Job queue and the download → upload pipeline."""

from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import Settings
from .downloader import Downloader
from .errors import (
    DownloadError,
    FileTooLarge,
    JobCancelled,
    JobTimeout,
    MirrorError,
    NoFallbackResolutionError,
    ResolutionError,
    UploaderUnavailable,
)
from .resolver import ResolvedLink, Resolver
from .uploaders import UploadResult, get_uploader
from .utils import CancelToken, human_size, safe_workdir

logger = logging.getLogger(__name__)

FINISHED_STATES = {"done", "failed", "cancelled"}


@dataclass
class Job:
    id: str
    user_id: int
    chat_id: int
    source: str
    host: str
    source_kind: str = "url"  # url | torrent_file | telegram_file
    local_path: Optional[Path] = None
    status: str = "queued"
    stage: str = "queued"
    percent: Optional[float] = None
    detail: str = ""
    file_name: Optional[str] = None
    file_size: Optional[int] = None
    extras: int = 0
    result: Optional[UploadResult] = None
    error: Optional[str] = None
    cancel: CancelToken = field(default_factory=CancelToken)
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def update(self, **values) -> None:
        with self._lock:
            for key, value in values.items():
                setattr(self, key, value)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "user_id": self.user_id,
                "chat_id": self.chat_id,
                "source": self.source,
                "host": self.host,
                "status": self.status,
                "stage": self.stage,
                "percent": self.percent,
                "detail": self.detail,
                "file_name": self.file_name,
                "file_size": self.file_size,
                "extras": self.extras,
                "result": self.result,
                "error": self.error,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }

    @property
    def finished(self) -> bool:
        with self._lock:
            return self.status in FINISHED_STATES

    @property
    def cancelled(self) -> bool:
        return self.cancel.cancelled


class JobManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.jobs: dict[str, Job] = {}
        self._tasks: set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ public
    async def submit(self, job: Job) -> None:
        async with self._lock:
            self.jobs[job.id] = job
        self._purge_old()
        task = asyncio.create_task(self._run(job), name=f"mirror-job-{job.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def get(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)

    def active_jobs(self) -> list[Job]:
        return [job for job in self.jobs.values() if not job.finished]

    def active_for(self, user_id: int) -> list[Job]:
        return [job for job in self.active_jobs() if job.user_id == user_id]

    def cancel_user(self, user_id: int) -> int:
        jobs = self.active_for(user_id)
        for job in jobs:
            job.cancel.cancel()
        return len(jobs)

    # --------------------------------------------------------------- internals
    async def _run(self, job: Job) -> None:
        async with self._semaphore:
            job.update(
                status="running",
                stage="resolving",
                started_at=time.time(),
                detail="Starting…",
            )
            job.cancel.deadline = time.monotonic() + self.settings.job_timeout
            try:
                await asyncio.to_thread(self._pipeline, job)
                job.update(status="done", stage="done", percent=100.0, detail="")
                logger.info("Job %s finished: %s", job.id, job.result.url if job.result else "")
            except JobCancelled:
                job.update(status="cancelled", stage="cancelled", detail="Cancelled.")
                logger.info("Job %s cancelled", job.id)
            except JobTimeout:
                job.update(
                    status="failed",
                    stage="failed",
                    error=f"Job timed out after {self.settings.job_timeout // 60} minutes.",
                )
                logger.warning("Job %s timed out", job.id)
            except MirrorError as exc:
                job.update(status="failed", stage="failed", error=str(exc))
                logger.warning("Job %s failed (%s)", job.id, type(exc).__name__)
                logger.debug("Job %s error detail: %s", job.id, exc)
            except Exception:
                logger.exception("Job %s crashed", job.id)
                job.update(
                    status="failed",
                    stage="failed",
                    error="Unexpected error. Check the bot logs for details.",
                )
            finally:
                job.update(finished_at=time.time())

    def _pipeline(self, job: Job) -> None:
        job.cancel.check()
        workdir = safe_workdir(self.settings.download_dir, job.id)
        try:
            if job.source_kind == "telegram_file":
                if not job.local_path or not job.local_path.exists():
                    raise DownloadError("The Telegram file is no longer available.")
                file_path = job.local_path
                file_size = file_path.stat().st_size
                extras = 0
                if file_size == 0:
                    raise DownloadError("The Telegram file is empty.")
                if file_size > self.settings.max_file_size:
                    raise FileTooLarge(
                        f"The file is {human_size(file_size)}, above the "
                        f"{human_size(self.settings.max_file_size)} limit."
                    )
            else:
                if job.source_kind == "torrent_file":
                    if not job.local_path or not job.local_path.exists():
                        raise DownloadError("The uploaded .torrent file is no longer available.")
                    link = ResolvedLink(
                        url=str(job.local_path.resolve()),
                        filename=job.local_path.name,
                        kind="torrent",
                    )
                else:
                    job.update(stage="resolving", detail="Looking for the direct link…")
                    resolver = Resolver(
                        allow_private_urls=self.settings.allow_private_urls,
                        gofile_token=self.settings.gofile_token,
                    )
                    try:
                        link = resolver.resolve(job.source)
                    except NoFallbackResolutionError:
                        raise
                    except ResolutionError as exc:
                        logger.info("Resolution failed (%s); trying yt-dlp", type(exc).__name__)
                        job.update(stage="resolving", detail="Trying the video extractor…")
                        link = ResolvedLink(url=job.source, kind="ytdlp")

                job.cancel.check()
                if not link.parts and link.size and link.size > self.settings.max_file_size:
                    raise FileTooLarge(
                        f"The file is {human_size(link.size)}, above the "
                        f"{human_size(self.settings.max_file_size)} limit."
                    )

                job.update(stage="downloading", percent=0.0, detail="Starting download…")
                downloader = Downloader(self.settings, job.cancel)
                downloaded = downloader.download(
                    original_url=job.source,
                    link=link,
                    dest=workdir / "files",
                    progress=lambda percent, detail: job.update(
                        stage="downloading", percent=percent, detail=detail
                    ),
                )

                if downloaded.size == 0:
                    raise DownloadError("The downloaded file is empty.")
                if downloaded.size > self.settings.max_file_size:
                    raise FileTooLarge(
                        f"The file is {human_size(downloaded.size)}, above the "
                        f"{human_size(self.settings.max_file_size)} limit."
                    )
                file_path = downloaded.path
                file_size = downloaded.size
                extras = downloaded.extras

            job.update(file_name=file_path.name, file_size=file_size, extras=extras)

            uploader = get_uploader(job.host, self.settings)
            usable, reason = uploader.available()
            if not usable:
                raise UploaderUnavailable(reason)

            job.update(
                stage="uploading",
                percent=0.0,
                detail=f"Uploading {human_size(file_size)} to {uploader.name.title()}…",
            )
            result = uploader.upload(
                file_path,
                progress=lambda percent, detail: job.update(
                    stage="uploading", percent=percent, detail=detail
                ),
                cancel=job.cancel,
            )
            job.update(result=result)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            if job.local_path and job.source_kind in {"torrent_file", "telegram_file"}:
                job.local_path.unlink(missing_ok=True)
                try:
                    job.local_path.parent.rmdir()
                except OSError:
                    pass

    def _purge_old(self) -> None:
        cutoff = time.time() - 6 * 60 * 60
        stale = [
            job_id
            for job_id, job in self.jobs.items()
            if job.finished and (job.finished_at or job.created_at) < cutoff
        ]
        for job_id in stale:
            self.jobs.pop(job_id, None)


def new_job(
    *,
    user_id: int,
    chat_id: int,
    source: str,
    host: str,
    source_kind: str = "url",
    local_path: Optional[Path] = None,
) -> Job:
    return Job(
        id=secrets.token_hex(4),
        user_id=user_id,
        chat_id=chat_id,
        source=source,
        host=host,
        source_kind=source_kind,
        local_path=local_path,
    )
