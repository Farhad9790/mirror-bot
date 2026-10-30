from __future__ import annotations

import pytest

from mirror.errors import UploadError
from mirror.uploaders.telegram import TelegramUploader, _iter_parts
from mirror.utils import CancelToken


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.headers: dict[str, str] = {}
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, data=None, headers=None, timeout=None):
        fields = getattr(getattr(data, "encoder", None), "fields", {})
        self.calls.append({"url": url, "headers": headers, "fields": fields})
        while True:
            chunk = data.read(64 * 1024)
            if not chunk:
                break
        return self.responses.pop(0)


def ok_response() -> FakeResponse:
    return FakeResponse(200, {"ok": True, "result": {"document": {"file_id": "x"}}})


def test_telegram_requires_token():
    uploader = TelegramUploader(None, 123)
    usable, reason = uploader.available()
    assert not usable
    assert "token" in reason.lower()


def test_telegram_requires_chat_id(tmp_path):
    source = tmp_path / "hello.txt"
    source.write_text("hello")
    uploader = TelegramUploader("token", None)
    with pytest.raises(UploadError):
        uploader.upload(source, lambda percent, detail: None, CancelToken())


def test_telegram_direct_send(tmp_path):
    source = tmp_path / "hello.txt"
    source.write_text("hello world")
    session = FakeSession([ok_response()])
    uploader = TelegramUploader(
        "token",
        123,
        max_upload=100,
        part_size=50,
        session=session,
    )
    updates: list[tuple] = []
    result = uploader.upload(
        source, lambda percent, detail: updates.append((percent, detail)), CancelToken()
    )

    assert result.host == "Telegram"
    assert result.url == ""
    assert result.detail == ""
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"].endswith("/bottoken/sendDocument")
    assert call["fields"]["chat_id"] == "123"
    assert call["fields"]["document"][0] == "hello.txt"
    assert call["fields"]["caption"].startswith("📄")
    assert updates
    assert updates[-1][0] == 100.0
    assert updates[-1][1] == "11 B / 11 B"


def test_telegram_splits_large_file(tmp_path):
    source = tmp_path / "hello.txt"
    source.write_bytes(b"0123456789")
    session = FakeSession([ok_response(), ok_response(), ok_response()])
    uploader = TelegramUploader(
        "token",
        123,
        max_upload=4,
        part_size=4,
        session=session,
    )
    result = uploader.upload(
        source, lambda percent, detail: None, CancelToken()
    )

    assert result.detail == "Sent as 3 parts."
    assert len(session.calls) == 3
    filenames = [call["fields"]["document"][0] for call in session.calls]
    assert filenames == [
        "hello.txt.part001",
        "hello.txt.part002",
        "hello.txt.part003",
    ]
    assert "part 1/3" in session.calls[0]["fields"]["caption"]
    assert "copy /b" in session.calls[0]["fields"]["caption"]
    assert "part 2/3" in session.calls[1]["fields"]["caption"]
    assert not list(tmp_path.glob(".telegram-parts-*"))


def test_telegram_retries_rate_limit(tmp_path, monkeypatch):
    source = tmp_path / "hello.txt"
    source.write_text("hello")
    session = FakeSession(
        [
            FakeResponse(429, {"ok": False, "parameters": {"retry_after": 1}}),
            ok_response(),
        ]
    )
    monkeypatch.setattr(
        "mirror.uploaders.telegram._sleep",
        lambda seconds, cancel: None,
    )
    uploader = TelegramUploader(
        "token",
        123,
        max_upload=100,
        part_size=50,
        session=session,
    )
    result = uploader.upload(source, lambda percent, detail: None, CancelToken())
    assert result.host == "Telegram"
    assert len(session.calls) == 2


def test_telegram_rejects_error_response(tmp_path):
    source = tmp_path / "hello.txt"
    source.write_text("hello")
    session = FakeSession(
        [FakeResponse(400, {"ok": False, "description": "Bad Request: file is too big"})]
    )
    uploader = TelegramUploader(
        "token",
        123,
        max_upload=100,
        part_size=50,
        session=session,
    )
    with pytest.raises(UploadError, match="too big"):
        uploader.upload(source, lambda percent, detail: None, CancelToken())


def test_iter_parts_reads_from_beginning(tmp_path):
    source = tmp_path / "data.bin"
    source.write_bytes(b"0123456789")
    parts_dir = tmp_path / "parts"
    parts_dir.mkdir()

    parts = list(_iter_parts(source, 4, CancelToken(), parts_dir))

    assert [part.name for _, part in parts] == [
        "data.bin.part001",
        "data.bin.part002",
        "data.bin.part003",
    ]
    assert [part.stat().st_size for _, part in parts] == [4, 4, 2]
    assert b"".join(part.read_bytes() for _, part in parts) == b"0123456789"
