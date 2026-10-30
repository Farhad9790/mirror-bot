from __future__ import annotations

import responses

from mirror.config import Settings
from mirror.downloader import Downloader
from mirror.resolver import ResolvedLink
from mirror.utils import CancelToken


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "telegram_token": "test",
        "download_dir": tmp_path,
        "max_file_size": 10 * 1024**2,
    }
    values.update(overrides)
    return Settings(**values)


def test_collect_picks_largest_file(tmp_path):
    dest = tmp_path / "files"
    dest.mkdir()
    (dest / "small.txt").write_text("small")
    (dest / "large.bin").write_bytes(b"x" * 1000)
    downloader = Downloader(make_settings(tmp_path), CancelToken())
    result = downloader._collect(dest)
    assert result.path.name == "large.bin"
    assert result.size == 1000
    assert result.extras == 1


def test_collect_zip_mode(tmp_path):
    dest = tmp_path / "files"
    dest.mkdir()
    (dest / "a.txt").write_text("a")
    (dest / "b.txt").write_text("b")
    downloader = Downloader(make_settings(tmp_path, multi_file_mode="zip"), CancelToken())
    result = downloader._collect(dest)
    assert result.path.suffix == ".zip"
    assert result.path.exists()
    assert not dest.exists()


@responses.activate
def test_requests_download(tmp_path):
    responses.add(
        responses.GET,
        "https://example.com/file.bin",
        body=b"hello",
        headers={"Content-Type": "application/octet-stream", "Content-Length": "5"},
    )
    updates = []
    downloader = Downloader(make_settings(tmp_path), CancelToken())
    result = downloader._requests_download(
        ResolvedLink(url="https://example.com/file.bin", filename="file.bin"),
        tmp_path,
        lambda percent, detail: updates.append((percent, detail)),
    )
    assert result.path.read_bytes() == b"hello"
    assert result.size == 5
    assert updates
    assert updates[-1][0] == 100.0


@responses.activate
def test_requests_download_rejects_html(tmp_path):
    responses.add(
        responses.GET,
        "https://example.com/page",
        body=b"<html>not a file</html>",
        headers={"Content-Type": "text/html"},
    )
    downloader = Downloader(make_settings(tmp_path), CancelToken())
    try:
        downloader._requests_download(
            ResolvedLink(url="https://example.com/page", filename="page.html"),
            tmp_path,
            lambda percent, detail: None,
        )
    except Exception as exc:
        assert "web page" in str(exc)
    else:
        raise AssertionError("expected a download error")
