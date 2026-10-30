from __future__ import annotations

import pytest

import mirror.jobs as jobs_module
from mirror.config import Settings
from mirror.errors import DownloadError, NoFallbackResolutionError
from mirror.jobs import JobManager, new_job
from mirror.uploaders.base import UploadResult


def test_gofile_resolution_error_does_not_fall_back_to_ytdlp(tmp_path, monkeypatch):
    settings = Settings(telegram_token="test", download_dir=tmp_path)
    manager = JobManager(settings)
    job = new_job(
        user_id=1,
        chat_id=1,
        source="https://gofile.io/d/broken",
        host="gofile",
    )

    def fail(self, url, depth=0):
        raise NoFallbackResolutionError(
            "That Gofile link does not exist or has expired."
        )

    monkeypatch.setattr(jobs_module.Resolver, "resolve", fail)

    with pytest.raises(NoFallbackResolutionError, match="Gofile"):
        manager._pipeline(job)


def test_telegram_file_is_uploaded_without_resolving(tmp_path, monkeypatch):
    settings = Settings(telegram_token="test", download_dir=tmp_path)
    manager = JobManager(settings)
    source = tmp_path / "uploads" / "token" / "report.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"pdf-data")

    class FakeUploader:
        name = "gofile"

        def available(self):
            return True, ""

        def upload(self, path, progress, cancel):
            assert path.read_bytes() == b"pdf-data"
            return UploadResult(url="https://gofile.io/d/telegram", host="Gofile")

    monkeypatch.setattr(jobs_module, "get_uploader", lambda host, cfg: FakeUploader())

    job = new_job(
        user_id=1,
        chat_id=1,
        source="report.pdf",
        host="gofile",
        source_kind="telegram_file",
        local_path=source,
    )
    manager._pipeline(job)

    assert job.result is not None
    assert job.result.url == "https://gofile.io/d/telegram"
    assert job.file_name == "report.pdf"
    assert job.file_size == 8
    assert not source.exists()
    assert not source.parent.exists()


def test_telegram_file_missing_on_disk_fails(tmp_path, monkeypatch):
    settings = Settings(telegram_token="test", download_dir=tmp_path)
    manager = JobManager(settings)
    job = new_job(
        user_id=1,
        chat_id=1,
        source="gone.bin",
        host="gofile",
        source_kind="telegram_file",
        local_path=tmp_path / "uploads" / "gone.bin",
    )

    with pytest.raises(DownloadError, match="no longer available"):
        manager._pipeline(job)
