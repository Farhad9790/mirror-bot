from __future__ import annotations

import pytest

from mirror.errors import ResolutionError
from mirror.resolver import Resolver


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

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, *, get=None, head=None) -> None:
        self.headers: dict = {}
        self._get = get
        self._head = head
        self.get_calls: list[str] = []
        self.head_calls: list[str] = []

    def get(self, url, **kwargs):
        self.get_calls.append(url)
        return self._get.pop(0) if isinstance(self._get, list) else self._get

    def head(self, url, **kwargs):
        self.head_calls.append(url)
        return self._head.pop(0) if isinstance(self._head, list) else self._head


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
