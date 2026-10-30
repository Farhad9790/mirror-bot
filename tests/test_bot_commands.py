from __future__ import annotations

import asyncio
from types import SimpleNamespace

import bot
from mirror.config import Settings


class FakeUser:
    id = 1


class FakeChat:
    id = 2


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self) -> None:
        self.effective_user = FakeUser()
        self.effective_chat = FakeChat()
        self.effective_message = FakeMessage()
        self.callback_query = None


class FakeContext:
    def __init__(self, settings: Settings, args: list[str]) -> None:
        self.application = SimpleNamespace(bot_data={"settings": settings})
        self.args = args


def run_direct(monkeypatch, host: str, command: str, args: list[str]):
    settings = Settings(telegram_token="token", allowed_user_ids=frozenset({1}))
    update = FakeUpdate()
    context = FakeContext(settings, args)
    started = []

    async def fake_start(context, job, message):
        started.append(job)

    monkeypatch.setattr(bot, "_start_job", fake_start)
    asyncio.run(bot._mirror_direct(update, context, host=host, command=command))
    return update, started


def test_gf_shortcut_starts_gofile_job(monkeypatch):
    update, started = run_direct(
        monkeypatch, "gofile", "gf", ["https://example.com/file.zip"]
    )
    assert len(started) == 1
    assert started[0].host == "gofile"
    assert started[0].source == "https://example.com/file.zip"
    assert not update.effective_message.replies


def test_l_shortcut_starts_telegram_job(monkeypatch):
    update, started = run_direct(
        monkeypatch, "telegram", "l", ["https://example.com/file.zip"]
    )
    assert len(started) == 1
    assert started[0].host == "telegram"
    assert not update.effective_message.replies


def test_shortcut_without_link_shows_usage(monkeypatch):
    update, started = run_direct(monkeypatch, "gofile", "gf", [])
    assert not started
    assert update.effective_message.replies
    assert "/gf" in update.effective_message.replies[0]
