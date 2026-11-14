from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telegram.constants import ChatType

import bot
from mirror.config import Settings


class FakeUser:
    id = 1


class FakeChat:
    def __init__(self, chat_type: str = ChatType.PRIVATE) -> None:
        self.id = 2
        self.type = chat_type


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, user_id: int = 1, chat_type: str = ChatType.PRIVATE) -> None:
        self.effective_user = SimpleNamespace(id=user_id)
        self.effective_chat = FakeChat(chat_type)
        self.effective_message = FakeMessage()
        self.callback_query = None


class FakeContext:
    def __init__(self, settings: Settings, args: list[str]) -> None:
        self.application = SimpleNamespace(bot_data={"settings": settings})
        self.args = args


def run_command(
    monkeypatch,
    command: str,
    args: list[str],
    *,
    user_id: int = 1,
    chat_type: str = ChatType.PRIVATE,
    **settings_kwargs,
):
    settings = Settings(
        telegram_token="token",
        allowed_user_ids=frozenset({1}),
        **settings_kwargs,
    )
    update = FakeUpdate(user_id=user_id, chat_type=chat_type)
    context = FakeContext(settings, args)
    started = []

    async def fake_start(context, job, message):
        started.append(job)

    monkeypatch.setattr(bot, "_start_job", fake_start)
    if command == "px":
        asyncio.run(bot.cmd_px(update, context))
    else:
        asyncio.run(bot._cmd_link(update, context, command=command))
    return update, started


def test_gf_starts_gofile_job(monkeypatch):
    update, started = run_command(monkeypatch, "gf", ["https://example.com/file.zip"])
    assert len(started) == 1
    assert started[0].host == "gofile"
    assert started[0].source == "https://example.com/file.zip"
    assert not update.effective_message.replies


def test_mirror_starts_gofile_job(monkeypatch):
    update, started = run_command(
        monkeypatch, "mirror", ["https://example.com/file.zip"]
    )
    assert len(started) == 1
    assert started[0].host == "gofile"
    assert not update.effective_message.replies


def test_command_without_link_shows_usage(monkeypatch):
    update, started = run_command(monkeypatch, "gf", [])
    assert not started
    assert update.effective_message.replies
    assert "/gf" in update.effective_message.replies[0]


def test_px_starts_pixeldrain_job(monkeypatch):
    update, started = run_command(
        monkeypatch,
        "px",
        ["https://example.com/file.zip"],
        pixeldrain_api_key="secret",
    )
    assert len(started) == 1
    assert started[0].host == "pixeldrain"
    assert started[0].source == "https://example.com/file.zip"
    assert not update.effective_message.replies


def test_px_requires_api_key(monkeypatch):
    update, started = run_command(monkeypatch, "px", ["https://example.com/file.zip"])
    assert not started
    assert update.effective_message.replies
    assert "PIXELDRAIN_API_KEY" in update.effective_message.replies[0]


def test_owner_can_use_private_chat(monkeypatch):
    update, started = run_command(monkeypatch, "gf", ["https://example.com/file.zip"])
    assert len(started) == 1
    assert not update.effective_message.replies


def test_stranger_dm_is_denied(monkeypatch):
    update, started = run_command(
        monkeypatch,
        "gf",
        ["https://example.com/file.zip"],
        user_id=99,
    )
    assert not started
    assert update.effective_message.replies
    assert "private" in update.effective_message.replies[0].lower()


def test_group_members_can_use_the_bot(monkeypatch):
    update, started = run_command(
        monkeypatch,
        "gf",
        ["https://example.com/file.zip"],
        user_id=99,
        chat_type=ChatType.GROUP,
    )
    assert len(started) == 1
    assert not update.effective_message.replies


def test_supergroup_members_can_use_the_bot(monkeypatch):
    update, started = run_command(
        monkeypatch,
        "mirror",
        ["https://example.com/file.zip"],
        user_id=99,
        chat_type=ChatType.SUPERGROUP,
    )
    assert len(started) == 1
    assert not update.effective_message.replies


def test_channels_are_denied(monkeypatch):
    update, started = run_command(
        monkeypatch,
        "gf",
        ["https://example.com/file.zip"],
        user_id=99,
        chat_type=ChatType.CHANNEL,
    )
    assert not started
    assert update.effective_message.replies


def test_id_is_denied_for_strangers():
    settings = Settings(telegram_token="token", allowed_user_ids=frozenset({1}))
    update = FakeUpdate(user_id=99)
    context = FakeContext(settings, [])
    asyncio.run(bot.cmd_id(update, context))
    assert update.effective_message.replies
    assert "private" in update.effective_message.replies[0].lower()
