from __future__ import annotations

import asyncio
import datetime
from types import SimpleNamespace

import pytest
from telegram import Chat, Message, MessageEntity, Update, User
from telegram.constants import ChatType
from telegram.ext import Application, ApplicationHandlerStop

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


class FakeMember:
    def __init__(self, status: str) -> None:
        self.status = status


class FakeBot:
    def __init__(self, statuses: dict[int, str] | None = None) -> None:
        self.statuses = statuses or {}

    async def get_chat_member(self, chat_id: int, user_id: int) -> FakeMember:
        return FakeMember(self.statuses.get(user_id, "left"))


class FakeContext:
    def __init__(
        self,
        settings: Settings,
        args: list[str],
        bot_instance: FakeBot | None = None,
    ) -> None:
        self.application = SimpleNamespace(bot_data={"settings": settings})
        self.args = args
        self.bot = bot_instance or FakeBot()


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


def test_group_gate_allows_group_with_owner():
    settings = Settings(telegram_token="token", allowed_user_ids=frozenset({1}))
    update = FakeUpdate(user_id=99, chat_type=ChatType.GROUP)
    context = FakeContext(settings, [], FakeBot({1: "member"}))
    asyncio.run(bot.gate_group_access(update, context))


def test_group_gate_blocks_group_without_owner():
    settings = Settings(telegram_token="token", allowed_user_ids=frozenset({1}))
    update = FakeUpdate(user_id=99, chat_type=ChatType.SUPERGROUP)
    context = FakeContext(settings, [], FakeBot({1: "left"}))
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(bot.gate_group_access(update, context))


def test_group_gate_ignores_private_chats():
    settings = Settings(telegram_token="token", allowed_user_ids=frozenset({1}))
    update = FakeUpdate(user_id=99)
    context = FakeContext(settings, [], FakeBot({1: "left"}))
    asyncio.run(bot.gate_group_access(update, context))


def _build_update(
    chat_type: str,
    *,
    user_id: int = 1,
    text: str = "/start",
    update_id: int = 1,
) -> Update:
    command = text.split()[0]
    message = Message(
        message_id=update_id,
        date=datetime.datetime.now(datetime.timezone.utc),
        chat=Chat(id=user_id, type=chat_type),
        from_user=User(id=user_id, first_name="User", is_bot=False),
        text=text,
        entities=[
            MessageEntity(
                type=MessageEntity.BOT_COMMAND,
                offset=0,
                length=len(command),
            )
        ],
    )
    return Update(update_id=update_id, message=message)


def _build_dispatcher(monkeypatch, tmp_path, started):
    app = Application.builder().token("123:abc").build()
    app._initialized = True
    app.bot._bot_user = User(
        id=999, first_name="Bot", is_bot=True, username="testbot"
    )
    settings = Settings(
        telegram_token="token",
        allowed_user_ids=frozenset({1}),
        state_file=tmp_path / "telegram.json",
    )

    async def fake_start(update, context):
        started.append(update)

    monkeypatch.setattr(bot, "cmd_start", fake_start)
    bot._register(app, settings, object())
    return app


def _dispatch(app: Application, update: Update) -> None:
    update.message.set_bot(app.bot)
    asyncio.run(app.process_update(update))


def test_gate_does_not_swallow_private_commands(monkeypatch, tmp_path):
    started = []
    app = _build_dispatcher(monkeypatch, tmp_path, started)
    _dispatch(app, _build_update(ChatType.PRIVATE))
    assert len(started) == 1


def test_gate_blocks_group_commands_before_handlers(monkeypatch, tmp_path):
    started = []
    app = _build_dispatcher(monkeypatch, tmp_path, started)

    async def no_owner(context, chat_id, settings):
        return False

    monkeypatch.setattr(bot, "_group_has_owner", no_owner)
    _dispatch(app, _build_update(ChatType.GROUP, user_id=99))
    assert not started
