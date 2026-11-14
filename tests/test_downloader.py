from __future__ import annotations

import responses

from mirror.config import Settings
from mirror.downloader import (
    Downloader,
    DownloadResult,
    _is_gofile_url,
    _looks_like_rate_limit,
)
from mirror.errors import DownloadError
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
def test_requests_download_with_no_size_limit(tmp_path):
    responses.add(
        responses.GET,
        "https://example.com/unlimited.bin",
        body=b"hello",
        headers={"Content-Type": "application/octet-stream", "Content-Length": "5"},
    )
    downloader = Downloader(make_settings(tmp_path, max_file_size=0), CancelToken())
    result = downloader._requests_download(
        ResolvedLink(url="https://example.com/unlimited.bin", filename="unlimited.bin"),
        tmp_path,
        lambda percent, detail: None,
    )
    assert result.path.read_bytes() == b"hello"
    assert result.size == 5


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


@responses.activate
def test_requests_download_sends_auth_headers(tmp_path):
    responses.add(
        responses.GET,
        "https://store.gofile.io/file.bin",
        body=b"data",
        headers={"Content-Type": "application/octet-stream", "Content-Length": "4"},
    )
    downloader = Downloader(make_settings(tmp_path), CancelToken())
    downloader._requests_download(
        ResolvedLink(
            url="https://store.gofile.io/file.bin",
            filename="file.bin",
            headers={"Authorization": "Bearer guest-token"},
        ),
        tmp_path,
        lambda percent, detail: None,
    )
    assert responses.calls[0].request.headers["Authorization"] == "Bearer guest-token"


@responses.activate
def test_requests_download_allows_forced_html(tmp_path):
    responses.add(
        responses.GET,
        "https://example.com/report.html",
        body=b"<html>saved</html>",
        headers={
            "Content-Type": "text/html",
            "Content-Disposition": 'attachment; filename="report.html"',
        },
    )
    downloader = Downloader(make_settings(tmp_path), CancelToken())
    result = downloader._requests_download(
        ResolvedLink(
            url="https://example.com/report.html",
            filename="report.html",
            force_download=True,
        ),
        tmp_path,
        lambda percent, detail: None,
    )
    assert result.path.read_bytes() == b"<html>saved</html>"


def test_aria2_http_sends_auth_headers(tmp_path, monkeypatch):
    downloader = Downloader(make_settings(tmp_path), CancelToken())
    captured = {}

    def fake_run(args, dest, progress, *, label):
        captured["args"] = args
        (dest / "file.bin").write_bytes(b"x" * 10)

    monkeypatch.setattr(downloader, "_run_aria2", fake_run)
    downloader._aria2_http(
        ResolvedLink(
            url="https://store.gofile.io/file.bin",
            filename="file.bin",
            headers={"Authorization": "Bearer guest-token"},
        ),
        tmp_path,
        lambda percent, detail: None,
    )
    assert "--header=Authorization: Bearer guest-token" in captured["args"]


def test_is_gofile_url():
    assert _is_gofile_url("https://gofile.io/download/abc")
    assert _is_gofile_url("https://store10.gofile.io/download/web/abc/file.zip")
    assert not _is_gofile_url("https://example.com/gofile.io/file")
    assert not _is_gofile_url("not a url")


def test_rate_limit_detection():
    assert _looks_like_rate_limit("aria2c failed: status=429")
    assert _looks_like_rate_limit("The response status is not successful. status=429")
    assert _looks_like_rate_limit("Gofile is rate-limiting downloads")
    assert not _looks_like_rate_limit(
        "aria2c: unrecognized option '--retry-on-http-error=429,500,502,503,504'"
    )
    assert not _looks_like_rate_limit("aria2c failed with exit code 1: time out")


def test_aria2_http_uses_gofile_safe_settings(tmp_path, monkeypatch):
    downloader = Downloader(make_settings(tmp_path), CancelToken())
    captured = {}

    def fake_run(args, dest, progress, *, label):
        captured["args"] = args
        (dest / "file.bin").write_bytes(b"x" * 10)

    monkeypatch.setattr(downloader, "_run_aria2", fake_run)
    downloader._aria2_http(
        ResolvedLink(url="https://store10.gofile.io/file.bin", filename="file.bin"),
        tmp_path,
        lambda percent, detail: None,
    )
    assert "--split=4" in captured["args"]
    assert "--max-connection-per-server=4" in captured["args"]
    assert "--max-tries=10" in captured["args"]
    # aria2 1.37 on GitHub runners rejects this option and exits immediately.
    assert not any(
        arg.startswith("--retry-on-http-error") for arg in captured["args"]
    )


def test_aria2_http_retries_gofile_429(tmp_path, monkeypatch):
    downloader = Downloader(make_settings(tmp_path), CancelToken())
    calls = []
    waits = []

    def fake_run(args, dest, progress, *, label):
        calls.append(label)
        if len(calls) == 1:
            raise DownloadError("aria2c failed with exit code 22: status=429")
        (dest / "file.bin").write_bytes(b"x" * 10)

    monkeypatch.setattr(downloader, "_run_aria2", fake_run)
    monkeypatch.setattr("mirror.downloader._wait", lambda cancel, seconds: waits.append(seconds))
    result = downloader._aria2_http(
        ResolvedLink(url="https://store10.gofile.io/file.bin", filename="file.bin"),
        tmp_path,
        lambda percent, detail: None,
    )
    assert len(calls) == 2
    assert waits == [30]
    assert result.size == 10


def test_aria2_http_reports_gofile_rate_limit(tmp_path, monkeypatch):
    downloader = Downloader(make_settings(tmp_path), CancelToken())

    def fake_run(args, dest, progress, *, label):
        raise DownloadError("aria2c failed with exit code 22: status=429")

    monkeypatch.setattr(downloader, "_run_aria2", fake_run)
    monkeypatch.setattr("mirror.downloader._wait", lambda cancel, seconds: None)
    try:
        downloader._aria2_http(
            ResolvedLink(url="https://store10.gofile.io/file.bin", filename="file.bin"),
            tmp_path,
            lambda percent, detail: None,
        )
    except DownloadError as exc:
        assert "rate-limiting" in str(exc)
        assert "5 attempts" in str(exc)
    else:
        raise AssertionError("expected a rate-limit error")


def test_aria2_http_does_not_retry_unrelated_errors(tmp_path, monkeypatch):
    downloader = Downloader(make_settings(tmp_path), CancelToken())
    calls = []
    waits = []

    def fake_run(args, dest, progress, *, label):
        calls.append(label)
        raise DownloadError(
            "aria2c: unrecognized option '--retry-on-http-error=429,500,502,503,504'"
        )

    monkeypatch.setattr(downloader, "_run_aria2", fake_run)
    monkeypatch.setattr("mirror.downloader._wait", lambda cancel, seconds: waits.append(seconds))
    try:
        downloader._aria2_http(
            ResolvedLink(url="https://store10.gofile.io/file.bin", filename="file.bin"),
            tmp_path,
            lambda percent, detail: None,
        )
    except DownloadError as exc:
        assert "unrecognized option" in str(exc)
    else:
        raise AssertionError("expected a download error")
    assert len(calls) == 1
    assert waits == []


@responses.activate
def test_requests_download_retries_429(tmp_path, monkeypatch):
    responses.add(responses.GET, "https://store10.gofile.io/file.bin", status=429)
    responses.add(responses.GET, "https://store10.gofile.io/file.bin", status=429)
    responses.add(
        responses.GET,
        "https://store10.gofile.io/file.bin",
        body=b"hello",
        headers={"Content-Type": "application/octet-stream", "Content-Length": "5"},
    )
    monkeypatch.setattr("mirror.downloader._wait", lambda cancel, seconds: None)
    downloader = Downloader(make_settings(tmp_path), CancelToken())
    result = downloader._requests_download(
        ResolvedLink(url="https://store10.gofile.io/file.bin", filename="file.bin"),
        tmp_path,
        lambda percent, detail: None,
    )
    assert result.path.read_bytes() == b"hello"
    assert len(responses.calls) == 3


@responses.activate
def test_requests_download_reports_gofile_rate_limit(tmp_path, monkeypatch):
    for _ in range(5):
        responses.add(responses.GET, "https://store10.gofile.io/file.bin", status=429)
    monkeypatch.setattr("mirror.downloader._wait", lambda cancel, seconds: None)
    downloader = Downloader(make_settings(tmp_path), CancelToken())
    try:
        downloader._requests_download(
            ResolvedLink(url="https://store10.gofile.io/file.bin", filename="file.bin"),
            tmp_path,
            lambda percent, detail: None,
        )
    except DownloadError as exc:
        assert "rate-limiting" in str(exc)
    else:
        raise AssertionError("expected a rate-limit error")


def test_download_parts_largest_mode(tmp_path, monkeypatch):
    downloader = Downloader(make_settings(tmp_path), CancelToken())
    downloaded = []

    def fake_http(link, dest, progress):
        downloaded.append(link.url)
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / link.filename
        path.write_bytes(b"x" * (link.size or 0))
        return DownloadResult(path=path, size=link.size or 0)

    monkeypatch.setattr(downloader, "_http", fake_http)
    parts = [
        ResolvedLink(url="https://example.com/small", filename="small.txt", size=5),
        ResolvedLink(url="https://example.com/large", filename="large.bin", size=100),
    ]
    link = ResolvedLink(url=parts[1].url, filename="large.bin", parts=parts)
    result = downloader._download_parts(link, tmp_path / "out", lambda percent, detail: None)

    assert downloaded == ["https://example.com/large"]
    assert result.path.name == "large.bin"
    assert result.size == 100
    assert result.extras == 1


def test_download_parts_zip_mode_downloads_all(tmp_path, monkeypatch):
    downloader = Downloader(
        make_settings(tmp_path, multi_file_mode="zip"), CancelToken()
    )
    downloaded = []

    def fake_http(link, dest, progress):
        downloaded.append(link.filename)
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / link.filename
        path.write_bytes(b"x" * (link.size or 0))
        return DownloadResult(path=path, size=link.size or 0)

    monkeypatch.setattr(downloader, "_http", fake_http)
    parts = [
        ResolvedLink(url="https://example.com/a", filename="a.txt", size=5),
        ResolvedLink(url="https://example.com/b", filename="b.bin", size=10),
    ]
    link = ResolvedLink(url=parts[0].url, filename="a.txt", parts=parts)
    result = downloader._download_parts(link, tmp_path / "out", lambda percent, detail: None)

    assert downloaded == ["a.txt", "b.bin"]
    assert result.path.suffix == ".zip"
    assert result.path.exists()
    assert result.size > 0
