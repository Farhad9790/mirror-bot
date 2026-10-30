from __future__ import annotations

import json

import pytest

import mirror.resolver as resolver_module
from mirror.errors import NoFallbackResolutionError, ResolutionError
from mirror.resolver import Resolver


@pytest.fixture(autouse=True)
def reset_gofile_token():
    resolver_module._gofile_token = None
    yield
    resolver_module._gofile_token = None


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        url: str = "https://example.com/file",
        headers: dict | None = None,
        body: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self.url = url
        self.headers = headers or {}
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size: int = 65536):
        for index in range(0, len(self._body), chunk_size):
            yield self._body[index : index + chunk_size]

    def json(self):
        if not self._body:
            raise ValueError("no json")
        return json.loads(self._body.decode("utf-8"))

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, *, get=None, head=None, post=None) -> None:
        self.headers: dict = {}
        self._get = get
        self._head = head
        self._post = post
        self.get_calls: list[str] = []
        self.head_calls: list[str] = []
        self.post_calls: list[str] = []
        self.last_headers: dict | None = None
        self.last_params: dict | None = None

    def get(self, url, **kwargs):
        self.get_calls.append(url)
        self.last_headers = kwargs.get("headers")
        self.last_params = kwargs.get("params")
        return self._get.pop(0) if isinstance(self._get, list) else self._get

    def head(self, url, **kwargs):
        self.head_calls.append(url)
        return self._head.pop(0) if isinstance(self._head, list) else self._head

    def post(self, url, **kwargs):
        self.post_calls.append(url)
        self.last_headers = kwargs.get("headers")
        return self._post.pop(0) if isinstance(self._post, list) else self._post


def gofile_json(data: dict) -> bytes:
    return json.dumps({"status": "ok", "data": data}).encode("utf-8")


def gofile_account(token: str) -> FakeResponse:
    return FakeResponse(
        body=json.dumps({"status": "ok", "data": {"token": token}}).encode("utf-8")
    )


def gofile_file(
    *,
    name: str = "README.rst",
    size: int = 8920,
    link: str = "https://store-eu-par-7.gofile.io/download/web/file-id/README.rst",
) -> dict:
    return {
        "canAccess": True,
        "type": "file",
        "id": "file-id",
        "code": "file-code",
        "name": name,
        "size": size,
        "mimetype": "text/plain; charset=utf-8",
        "link": link,
    }


def test_extract_candidates_download_button():
    html = b"""
    <html><head><title>My File</title></head><body>
      <a href="/files/report.pdf" class="download-button">Download</a>
      <a href="/about">About</a>
    </body></html>
    """
    resolver = Resolver(allow_private_urls=True)
    candidates, title = resolver._extract_candidates(html, "https://example.com/page")
    assert title == "My File"
    assert candidates[0][1] == "https://example.com/files/report.pdf"
    assert candidates[0][0] >= 8


def test_extract_candidates_data_and_media():
    html = b"""
    <html><body>
      <button data-download-url="https://cdn.example.com/video.mp4">Get file</button>
      <video src="/media/movie.webm"></video>
      <meta http-equiv="refresh" content="0; url=/next">
    </body></html>
    """
    resolver = Resolver(allow_private_urls=True)
    candidates, _ = resolver._extract_candidates(html, "https://example.com/page")
    urls = [url for _, url in candidates]
    assert "https://cdn.example.com/video.mp4" in urls
    assert "https://example.com/media/movie.webm" in urls
    assert "https://example.com/next" in urls


def test_extract_candidates_ignores_javascript_links():
    html = b'<html><body><a href="javascript:void(0)" download>Download</a></body></html>'
    resolver = Resolver(allow_private_urls=True)
    candidates, _ = resolver._extract_candidates(html, "https://example.com/page")
    assert candidates == []


def test_special_urls():
    resolver = Resolver(allow_private_urls=True)
    drive = resolver._special_url(
        "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/view?usp=sharing"
    )
    assert drive.startswith("https://drive.usercontent.google.com/download?id=")
    assert "confirm=t" in drive

    pixel = resolver._special_url("https://pixeldrain.com/u/abcd1234")
    assert pixel == "https://pixeldrain.com/api/file/abcd1234?download"

    dropbox = resolver._special_url("https://www.dropbox.com/s/abc/file.zip?dl=0")
    assert "dl=1" in dropbox

    with pytest.raises(ResolutionError):
        resolver._special_url("https://mega.nz/file/abc#key")


def test_resolve_html_file_served_as_attachment():
    response = FakeResponse(
        url="https://example.com/report.html",
        headers={
            "content-type": "text/html; charset=utf-8",
            "content-disposition": 'attachment; filename="report.html"',
            "content-length": "42",
        },
    )
    resolver = Resolver(allow_private_urls=True, session=FakeSession(get=response))
    link = resolver.resolve("https://example.com/report.html")
    assert link.kind == "http"
    assert link.filename == "report.html"
    assert link.force_download is True


def test_resolve_direct_file_of_any_type():
    response = FakeResponse(
        url="https://cdn.example.com/app.js",
        headers={"content-type": "application/javascript", "content-length": "42"},
    )
    resolver = Resolver(allow_private_urls=True, session=FakeSession(get=response))
    link = resolver.resolve("https://example.com/app.js")
    assert link.kind == "http"
    assert link.filename == "app.js"
    assert link.content_type == "application/javascript"


def test_resolve_direct_file():
    response = FakeResponse(
        url="https://cdn.example.com/archive.zip",
        headers={
            "content-type": "application/zip",
            "content-length": "1234",
            "content-disposition": 'attachment; filename="archive.zip"',
        },
    )
    session = FakeSession(get=response)
    resolver = Resolver(allow_private_urls=True, session=session)
    link = resolver.resolve("https://example.com/download")
    assert link.kind == "http"
    assert link.url == "https://cdn.example.com/archive.zip"
    assert link.filename == "archive.zip"
    assert link.size == 1234
    assert response.closed


def test_resolve_short_link_redirected_to_gofile():
    redirect = FakeResponse(
        url="https://gofile.io/d/nc6r8GH0",
        headers={"content-type": "text/html"},
        body=b"<html></html>",
    )
    session = FakeSession(
        post=gofile_account("guest-token"),
        get=[redirect, FakeResponse(body=gofile_json(gofile_file()))],
    )
    resolver = Resolver(allow_private_urls=True, session=session)
    link = resolver.resolve("https://short.example.com/abc")

    assert link.filename == "README.rst"
    assert link.headers == {"Authorization": "Bearer guest-token"}
    assert redirect.closed


def test_resolve_download_button_page():
    page = FakeResponse(
        url="https://example.com/page",
        headers={"content-type": "text/html; charset=utf-8"},
        body=b'<html><title>Report</title><a href="/files/report.pdf" download>Download</a></html>',
    )
    file_response = FakeResponse(
        url="https://example.com/files/report.pdf",
        headers={
            "content-type": "application/pdf",
            "content-length": "99",
            "content-disposition": 'attachment; filename="report.pdf"',
        },
    )
    session = FakeSession(get=page, head=file_response)
    resolver = Resolver(allow_private_urls=True, session=session)
    link = resolver.resolve("https://example.com/page")
    assert link.url == "https://example.com/files/report.pdf"
    assert link.filename == "report.pdf"
    assert link.referer == "https://example.com/page"
    assert session.head_calls == ["https://example.com/files/report.pdf"]


def test_resolve_page_without_download():
    page = FakeResponse(
        url="https://example.com/page",
        headers={"content-type": "text/html"},
        body=b"<html><body><p>Nothing here</p></body></html>",
    )
    resolver = Resolver(allow_private_urls=True, session=FakeSession(get=page))
    with pytest.raises(ResolutionError):
        resolver.resolve("https://example.com/page")


def test_gofile_code_formats():
    assert resolver_module._gofile_code("https://gofile.io/d/abc123") == "abc123"
    assert resolver_module._gofile_code("https://www.gofile.io/d/abc123?x=1") == "abc123"
    assert resolver_module._gofile_code("https://gofile.io/?c=legacy1") == "legacy1"
    assert resolver_module._gofile_code("https://example.com/d/abc123") is None


def test_resolve_gofile_single_file():
    session = FakeSession(
        post=gofile_account("guest-token"),
        get=FakeResponse(body=gofile_json(gofile_file())),
    )
    resolver = Resolver(allow_private_urls=True, session=session)
    link = resolver.resolve("https://gofile.io/d/nc6r8GH0")

    assert link.url == "https://store-eu-par-7.gofile.io/download/web/file-id/README.rst"
    assert link.filename == "README.rst"
    assert link.size == 8920
    assert link.parts == []
    assert link.headers == {"Authorization": "Bearer guest-token"}
    assert session.post_calls == ["https://api.gofile.io/accounts"]
    assert session.get_calls == ["https://api.gofile.io/contents/nc6r8GH0"]
    assert session.last_headers["Authorization"] == "Bearer guest-token"
    assert session.last_headers["X-BL"] == "en-US"
    assert len(session.last_headers["X-Website-Token"]) == 64
    assert session.last_params["pageSize"] == "100"


def test_resolve_gofile_uses_configured_token():
    session = FakeSession(get=FakeResponse(body=gofile_json(gofile_file())))
    resolver = Resolver(
        allow_private_urls=True,
        gofile_token="account-token",
        session=session,
    )
    link = resolver.resolve("https://gofile.io/d/nc6r8GH0")

    assert link.headers == {"Authorization": "Bearer account-token"}
    assert session.post_calls == []


def test_resolve_gofile_accepts_any_file_type():
    archive = gofile_file(
        name="archive.7z",
        size=123,
        link="https://store.gofile.io/download/web/7z/archive.7z",
    )
    archive["mimetype"] = "application/x-7z-compressed"
    session = FakeSession(
        post=gofile_account("guest-token"),
        get=FakeResponse(body=gofile_json(archive)),
    )
    resolver = Resolver(allow_private_urls=True, session=session)
    link = resolver.resolve("https://gofile.io/d/archive-code")

    assert link.filename == "archive.7z"
    assert link.content_type == "application/x-7z-compressed"
    assert link.allow_ytdlp_fallback is False


def test_resolve_gofile_folder_returns_all_parts():
    folder = {
        "canAccess": True,
        "type": "folder",
        "code": "hhDBtEO5",
        "children": {
            "small": gofile_file(
                name="small.txt",
                size=5,
                link="https://store.gofile.io/download/web/small/small.txt",
            ),
            "large": gofile_file(
                name="large.bin",
                size=500,
                link="https://store.gofile.io/download/web/large/large.bin",
            ),
        },
        "metadata": {"hasNextPage": False},
    }
    session = FakeSession(
        post=gofile_account("guest-token"),
        get=FakeResponse(body=gofile_json(folder)),
    )
    resolver = Resolver(allow_private_urls=True, session=session)
    link = resolver.resolve("https://gofile.io/d/hhDBtEO5")

    assert link.filename == "large.bin"
    assert link.size == 505
    assert [part.filename for part in link.parts] == ["large.bin", "small.txt"]
    assert link.headers == {"Authorization": "Bearer guest-token"}
    assert all(part.headers == link.headers for part in link.parts)
    assert session.get_calls == ["https://api.gofile.io/contents/hhDBtEO5"]


def test_resolve_gofile_nested_folder():
    top = {
        "canAccess": True,
        "type": "folder",
        "code": "TOP123",
        "children": {
            "sub": {
                "type": "folder",
                "id": "sub-id",
                "code": "SUB456",
                "name": "sub",
            }
        },
        "metadata": {"hasNextPage": False},
    }
    nested = {
        "canAccess": True,
        "type": "folder",
        "code": "SUB456",
        "children": {
            "file": gofile_file(
                name="nested.bin",
                size=77,
                link="https://store.gofile.io/download/web/nested/nested.bin",
            )
        },
        "metadata": {"hasNextPage": False},
    }
    session = FakeSession(
        post=gofile_account("guest-token"),
        get=[FakeResponse(body=gofile_json(top)), FakeResponse(body=gofile_json(nested))],
    )
    resolver = Resolver(allow_private_urls=True, session=session)
    link = resolver.resolve("https://gofile.io/d/TOP123")

    assert link.filename == "nested.bin"
    assert link.size == 77
    assert session.get_calls == [
        "https://api.gofile.io/contents/TOP123",
        "https://api.gofile.io/contents/SUB456",
    ]


def test_resolve_gofile_refreshes_rejected_token():
    rejected = FakeResponse(
        status_code=401,
        body=json.dumps({"status": "error-token", "data": {}}).encode("utf-8"),
    )
    session = FakeSession(
        post=[gofile_account("old-token"), gofile_account("new-token")],
        get=[rejected, FakeResponse(body=gofile_json(gofile_file()))],
    )
    resolver = Resolver(allow_private_urls=True, session=session)
    link = resolver.resolve("https://gofile.io/d/nc6r8GH0")

    assert link.headers == {"Authorization": "Bearer new-token"}
    assert len(session.post_calls) == 2


def test_resolve_gofile_password_protected():
    folder = {
        "canAccess": False,
        "type": "folder",
        "password": "a-hash",
        "passwordStatus": "set",
    }
    session = FakeSession(
        post=gofile_account("guest-token"),
        get=FakeResponse(body=gofile_json(folder)),
    )
    resolver = Resolver(allow_private_urls=True, session=session)
    with pytest.raises(NoFallbackResolutionError, match="password-protected"):
        resolver.resolve("https://gofile.io/d/locked")


def test_resolve_gofile_missing_link():
    session = FakeSession(
        post=gofile_account("guest-token"),
        get=FakeResponse(
            status_code=404,
            body=json.dumps({"status": "error-notFound", "data": {}}).encode("utf-8"),
        ),
    )
    resolver = Resolver(allow_private_urls=True, session=session)
    with pytest.raises(ResolutionError, match="does not exist"):
        resolver.resolve("https://gofile.io/d/gone")
