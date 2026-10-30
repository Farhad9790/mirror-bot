from __future__ import annotations

import pytest

from mirror.errors import UploadError, UploaderUnavailable
from mirror.uploaders.gofile import GofileUploader
from mirror.uploaders.pixeldrain import PixeldrainUploader
from mirror.utils import CancelToken


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers: dict[str, str] = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.headers: dict[str, str] = {}
        self.response = response
        self.last_url: str | None = None
        self.last_headers: dict | None = None
        self.last_auth = None
        self.last_data = None

    def _consume(self, data) -> None:
        self.last_data = data
        while True:
            chunk = data.read(64 * 1024)
            if not chunk:
                break

    def post(self, url, data=None, headers=None, timeout=None):
        self.last_url = url
        self.last_headers = headers
        self._consume(data)
        return self.response

    def put(self, url, data=None, headers=None, auth=None, timeout=None):
        self.last_url = url
        self.last_headers = headers
        self.last_auth = auth
        self._consume(data)
        return self.response


def test_gofile_guest_upload(tmp_path):
    source = tmp_path / "hello.txt"
    source.write_text("hello world")
    response = FakeResponse(
        200,
        {
            "status": "ok",
            "data": {"downloadPage": "https://gofile.io/d/abc123"},
        },
    )
    session = FakeSession(response)
    uploader = GofileUploader(session=session)
    result = uploader.upload(source, lambda percent, detail: None, CancelToken())
    assert result.url == "https://gofile.io/d/abc123"
    assert session.last_url == "https://upload.gofile.io/uploadfile"
    assert "Authorization" not in session.last_headers


def test_gofile_folder_fallback(tmp_path):
    source = tmp_path / "hello.txt"
    source.write_text("hello")
    response = FakeResponse(200, {"status": "ok", "data": {"parentFolderCode": "XYZ789"}})
    result = GofileUploader(session=FakeSession(response)).upload(
        source, lambda percent, detail: None, CancelToken()
    )
    assert result.url == "https://gofile.io/d/XYZ789"


def test_gofile_token_is_sent(tmp_path):
    source = tmp_path / "hello.txt"
    source.write_text("hello")
    response = FakeResponse(200, {"status": "ok", "data": {"downloadPage": "https://gofile.io/d/x"}})
    session = FakeSession(response)
    GofileUploader(token="secret", session=session).upload(
        source, lambda percent, detail: None, CancelToken()
    )
    assert session.last_headers["Authorization"] == "Bearer secret"


def test_gofile_rejects_bad_token(tmp_path):
    source = tmp_path / "hello.txt"
    source.write_text("hello")
    session = FakeSession(FakeResponse(401, text="unauthorized"))
    with pytest.raises(UploadError):
        GofileUploader(token="bad", session=session).upload(
            source, lambda percent, detail: None, CancelToken()
        )


def test_pixeldrain_requires_key(tmp_path):
    source = tmp_path / "hello.txt"
    source.write_text("hello")
    uploader = PixeldrainUploader(None)
    usable, reason = uploader.available()
    assert not usable
    assert "PIXELDRAIN_API_KEY" in reason
    with pytest.raises(UploaderUnavailable):
        uploader.upload(source, lambda percent, detail: None, CancelToken())


def test_pixeldrain_upload(tmp_path):
    source = tmp_path / "my file.txt"
    source.write_text("hello")
    response = FakeResponse(201, {"success": True, "id": "abc123"})
    session = FakeSession(response)
    uploader = PixeldrainUploader("api-key", session=session)
    result = uploader.upload(source, lambda percent, detail: None, CancelToken())
    assert result.url == "https://pixeldrain.com/u/abc123"
    assert session.last_url == "https://pixeldrain.com/api/file/my%20file.txt"
    assert session.last_auth == ("", "api-key")
    assert session.last_headers["Content-Length"] == "5"


def test_pixeldrain_rejects_bad_key(tmp_path):
    source = tmp_path / "hello.txt"
    source.write_text("hello")
    session = FakeSession(FakeResponse(401, text="authentication_required"))
    with pytest.raises(UploadError):
        PixeldrainUploader("bad", session=session).upload(
            source, lambda percent, detail: None, CancelToken()
        )
