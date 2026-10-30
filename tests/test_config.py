from __future__ import annotations

import pytest

from mirror.config import load_settings


def test_load_settings(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1, 2, -3")
    monkeypatch.setenv("MAX_FILE_SIZE", "1G")
    monkeypatch.setenv("DEFAULT_HOST", "pixeldrain")
    monkeypatch.setenv("MAX_CONCURRENT_JOBS", "3")
    settings = load_settings()
    assert settings.telegram_token == "123:abc"
    assert settings.allowed_user_ids == frozenset({1, 2, -3})
    assert settings.max_file_size == 1024**3
    assert settings.default_host == "pixeldrain"
    assert settings.max_concurrent_jobs == 3


def test_load_settings_falls_back_on_bad_values(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("DEFAULT_HOST", "nonsense")
    monkeypatch.setenv("MAX_CONCURRENT_JOBS", "0")
    monkeypatch.setenv("MULTI_FILE_MODE", "nonsense")
    settings = load_settings()
    assert settings.default_host == "gofile"
    assert settings.max_concurrent_jobs == 1
    assert settings.multi_file_mode == "largest"


def test_token_is_required(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        load_settings()
