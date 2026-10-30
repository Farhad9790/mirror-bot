from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import bot
from mirror.config import Settings


def fake_message(**overrides):
    values = {
        "photo": None,
        "video": None,
        "audio": None,
        "voice": None,
        "video_note": None,
        "animation": None,
        "sticker": None,
        "document": None,
        "text": None,
        "caption": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_attachment_filename_keeps_document_name():
    document = SimpleNamespace(
        file_name="My Report.pdf",
        mime_type="application/pdf",
        file_unique_id="doc-1",
    )
    assert bot._attachment_filename(fake_message(document=document), document) == "My Report.pdf"


def test_attachment_filename_names_photos_and_voice_notes():
    photo = SimpleNamespace(
        file_name=None,
        mime_type="image/jpeg",
        file_unique_id="photo-1",
    )
    voice = SimpleNamespace(
        file_name=None,
        mime_type="audio/ogg",
        file_unique_id="voice-1",
    )

    assert bot._attachment_filename(fake_message(photo=[photo]), photo) == "photo_photo-1.jpg"
    assert bot._attachment_filename(fake_message(voice=voice), voice) == "voice_voice-1.ogg"


def test_message_attachment_picks_largest_photo():
    small = SimpleNamespace(file_size=100, width=10, height=10)
    large = SimpleNamespace(file_size=900, width=30, height=30)
    message = fake_message(photo=[small, large])

    assert bot._message_attachment(message) is large


class FakeStatus:
    def __init__(self):
        self.edits: list[str] = []

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)


class FakeTelegramFile:
    def __init__(self, payload: bytes):
        self.payload = payload

    async def download_to_drive(self, custom_path):
        Path(custom_path).write_bytes(self.payload)


class FakeDocument:
    def __init__(self, name: str, payload: bytes, size: int | None = None, mime_type: str = "text/plain"):
        self.file_name = name
        self.file_size = len(payload) if size is None else size
        self.mime_type = mime_type
        self.file_unique_id = "doc-1"
        self.payload = payload

    async def get_file(self):
        return FakeTelegramFile(self.payload)


class FakeAttachmentMessage:
    def __init__(self, document: FakeDocument):
        self.document = document
        self.photo = None
        self.video = None
        self.audio = None
        self.voice = None
        self.video_note = None
        self.animation = None
        self.sticker = None
        self.replies: list[str] = []
        self.statuses: list[FakeStatus] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        status = FakeStatus()
        self.statuses.append(status)
        return status


def make_update(document: FakeDocument):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=2),
        effective_message=FakeAttachmentMessage(document),
        callback_query=None,
    )


class FakeCommandMessage:
    def __init__(self, replied_to=None):
        self.reply_to_message = replied_to
        self.text = "/gf"
        self.replies: list[str] = []
        self.statuses: list[FakeStatus] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        status = FakeStatus()
        self.statuses.append(status)
        return status


def make_context(tmp_path, args=None):
    settings = Settings(
        telegram_token="token",
        allowed_user_ids=frozenset({1}),
        download_dir=tmp_path,
    )
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"settings": settings}),
        args=list(args or []),
    )


def make_command_update(command_message: FakeCommandMessage):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=2),
        effective_message=command_message,
        callback_query=None,
    )


def test_attachment_is_saved_and_submitted_as_telegram_file(tmp_path, monkeypatch):
    update = make_update(FakeDocument("hello.txt", b"hello"))
    context = make_context(tmp_path)
    submitted = {}

    async def fake_submit(update, context, **kwargs):
        submitted.update(kwargs)

    monkeypatch.setattr(bot, "_submit_job", fake_submit)
    asyncio.run(bot.on_attachment(update, context))

    assert submitted["source"] == "hello.txt"
    assert submitted["source_kind"] == "telegram_file"
    assert submitted["local_path"].read_bytes() == b"hello"
    assert submitted["status_message"] is update.effective_message.statuses[0]


def test_torrent_document_keeps_torrent_job(tmp_path, monkeypatch):
    document = FakeDocument(
        "ubuntu.torrent",
        b"d4:infod4:name4:teste",
        mime_type="application/x-bittorrent",
    )
    update = make_update(document)
    context = make_context(tmp_path)
    submitted = {}

    async def fake_submit(update, context, **kwargs):
        submitted.update(kwargs)

    monkeypatch.setattr(bot, "_submit_job", fake_submit)
    asyncio.run(bot.on_attachment(update, context))

    assert submitted["source_kind"] == "torrent_file"
    assert submitted["local_path"].name == "ubuntu.torrent"


def test_large_telegram_attachment_is_rejected(tmp_path, monkeypatch):
    document = FakeDocument("big.bin", b"", size=21 * 1024 * 1024)
    update = make_update(document)
    context = make_context(tmp_path)
    submitted = {}

    async def fake_submit(update, context, **kwargs):
        submitted.update(kwargs)

    monkeypatch.setattr(bot, "_submit_job", fake_submit)
    asyncio.run(bot.on_attachment(update, context))

    assert not submitted
    assert "20 MB" in update.effective_message.replies[0]


def test_gf_reply_to_file_submits_telegram_file(tmp_path, monkeypatch):
    replied = FakeAttachmentMessage(FakeDocument("hello.txt", b"hello"))
    command = FakeCommandMessage(replied)
    update = make_command_update(command)
    context = make_context(tmp_path)
    submitted = {}

    async def fake_submit(update, context, **kwargs):
        submitted.update(kwargs)

    monkeypatch.setattr(bot, "_submit_job", fake_submit)
    asyncio.run(bot._cmd_link(update, context, command="gf"))

    assert submitted["source"] == "hello.txt"
    assert submitted["source_kind"] == "telegram_file"
    assert submitted["local_path"].read_bytes() == b"hello"
    assert submitted["status_message"] is command.statuses[0]


def test_mirror_reply_to_text_link_submits_link(tmp_path, monkeypatch):
    replied = fake_message(text="https://example.com/file.zip")
    command = FakeCommandMessage(replied)
    update = make_command_update(command)
    context = make_context(tmp_path)
    submitted = {}

    async def fake_submit(update, context, **kwargs):
        submitted.update(kwargs)

    monkeypatch.setattr(bot, "_submit_job", fake_submit)
    asyncio.run(bot._cmd_link(update, context, command="mirror"))

    assert submitted["source"] == "https://example.com/file.zip"
    assert "source_kind" not in submitted


def test_gf_reply_to_unsupported_message_shows_usage(tmp_path, monkeypatch):
    command = FakeCommandMessage(fake_message(text="hello"))
    update = make_command_update(command)
    context = make_context(tmp_path)

    asyncio.run(bot._cmd_link(update, context, command="gf"))

    assert "Usage" in command.replies[0]
    assert "reply to a file" in command.replies[0]
