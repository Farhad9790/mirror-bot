from __future__ import annotations

import pytest

import mirror.jobs as jobs_module
from mirror.config import Settings
from mirror.errors import NoFallbackResolutionError
from mirror.jobs import JobManager, new_job


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
