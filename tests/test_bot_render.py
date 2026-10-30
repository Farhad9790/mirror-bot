from __future__ import annotations

from bot import render_final, render_job
from mirror.config import Settings
from mirror.jobs import new_job
from mirror.uploaders.base import UploadResult


def test_render_running_job():
    job = new_job(user_id=1, chat_id=2, source="https://example.com/a.zip", host="gofile")
    job.update(status="running", stage="downloading", percent=42.0, detail="1.2 GB / 2.8 GB · 24 MB/s")
    text = render_job(job)
    assert "Downloading" in text
    assert "42%" in text
    assert "Gofile" in text
    assert "1.2 GB" in text


def test_render_done_job():
    job = new_job(user_id=1, chat_id=2, source="https://example.com/a.zip", host="gofile")
    job.update(
        status="done",
        stage="done",
        percent=100.0,
        file_name="a.zip",
        file_size=1234,
        result=UploadResult(url="https://gofile.io/d/abc", host="Gofile"),
    )
    text = render_final(job)
    assert "Mirror complete" in text
    assert "https://gofile.io/d/abc" in text
    assert "1.2 KB" in text


def test_render_failed_job_escapes_html():
    job = new_job(user_id=1, chat_id=2, source="https://example.com/a.zip", host="gofile")
    job.update(status="failed", stage="failed", error="bad <script> & stuff")
    text = render_final(job)
    assert "&lt;script&gt;" in text
    assert "&amp;" in text


def test_settings_defaults():
    settings = Settings(telegram_token="x")
    assert settings.max_concurrent_jobs == 2
    assert settings.gofile_token is None
