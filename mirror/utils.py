"""Shared helpers: URL handling, filenames, sizes and cancellation."""

from __future__ import annotations

import ipaddress
import math
import re
import socket
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse, urlunparse

from .errors import InvalidUrl, JobCancelled, JobTimeout

URL_RE = re.compile(r"(?:https?://|magnet:\?)[^\s<>\"']+", re.IGNORECASE)
MAGNET_RE = re.compile(r"^magnet:\?[^\s]+$", re.IGNORECASE)

# Characters that are illegal on Windows, plus control characters.
_ILLEGAL_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def extract_url(text: str) -> Optional[str]:
    """Return the first URL or magnet link in a message, if any."""
    match = URL_RE.search(text or "")
    return match.group(0).rstrip(").,;!?") if match else None


def is_magnet(link: str) -> bool:
    return bool(MAGNET_RE.match((link or "").strip()))


def normalize_url(link: str) -> str:
    """Add a scheme to bare host/path links and strip whitespace."""
    link = (link or "").strip()
    if not link:
        raise InvalidUrl("No link supplied.")
    if is_magnet(link):
        return link
    if not re.match(r"^[a-z][a-z0-9+.-]*://", link, re.IGNORECASE):
        link = "https://" + link
    return link


def human_size(size: Optional[float]) -> str:
    if size is None:
        return "?"
    if size < 0:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def human_speed(bytes_per_second: Optional[float]) -> str:
    if not bytes_per_second or bytes_per_second <= 0:
        return ""
    return f"{human_size(bytes_per_second)}/s"


def human_eta(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0 or math.isinf(seconds):
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def progress_bar(percent: Optional[float], width: int = 12) -> str:
    if percent is None:
        return "░" * width
    percent = max(0.0, min(100.0, float(percent)))
    filled = int(round(width * percent / 100.0))
    return "█" * filled + "░" * (width - filled)


def parse_size(value: str | int | None, default: int) -> int:
    """Parse values like 512M, 8G or plain bytes."""
    if value is None or value == "":
        return default
    if isinstance(value, int):
        return value
    text = str(value).strip().upper().replace("IB", "B")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGT]?B?)", text)
    if not match:
        return default
    number = float(match.group(1))
    unit = match.group(2).rstrip("B") or "B"
    factor = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}.get(unit, 1)
    return int(number * factor)


def sanitize_filename(name: str, fallback: str = "download.bin", max_length: int = 180) -> str:
    """Make a remote filename safe for Windows, Linux and Telegram."""
    name = unquote(name or "").strip()
    name = unicodedata.normalize("NFKC", name)
    name = _ILLEGAL_FILENAME.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name or name in {".", ".."}:
        name = fallback
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    stem = stem.strip(" .") or "file"
    if stem.upper() in _RESERVED_NAMES:
        stem = f"_{stem}"
    if len(suffix) > 16:
        suffix = ""
    keep = max(1, max_length - len(suffix) - (1 if suffix else 0))
    stem = stem[:keep]
    result = f"{stem}.{suffix}" if suffix else stem
    return result or fallback


def filename_from_url(url: str, fallback: str = "download.bin") -> str:
    path = urlparse(url).path
    name = unquote(path.rsplit("/", 1)[-1]) if path else ""
    if not name or name in {".", ".."}:
        return fallback
    return sanitize_filename(name, fallback)


def filename_from_headers(headers) -> Optional[str]:
    """Extract filename/filename* from a Content-Disposition header."""
    disposition = headers.get("content-disposition") if headers else None
    if not disposition:
        return None
    match = re.search(r"filename\*\s*=\s*([^;]+)", disposition, re.IGNORECASE)
    if match:
        value = match.group(1).strip().strip('"')
        if "''" in value:
            value = value.split("''", 1)[1]
        try:
            from urllib.parse import unquote as _unquote
            return sanitize_filename(_unquote(value))
        except Exception:
            pass
    match = re.search(r'filename\s*=\s*("([^"]*)"|[^;]+)', disposition, re.IGNORECASE)
    if match:
        value = (match.group(2) or match.group(1) or "").strip().strip('"')
        if value:
            return sanitize_filename(value)
    return None


def redact_url(url: str) -> str:
    """Remove query strings so tokens do not end up in logs."""
    try:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    except Exception:
        return "<url>"


def _is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global


def validate_http_url(url: str, allow_private: bool = False) -> None:
    """Block localhost/private-network URLs (basic SSRF protection)."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise InvalidUrl("Only http:// and https:// links are supported.")
    host = parsed.hostname
    if not host:
        raise InvalidUrl("That link has no hostname.")
    if not allow_private:
        if host.lower() in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            raise InvalidUrl("Local network addresses are not allowed.")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise InvalidUrl("That link has an invalid port.") from exc
        try:
            addresses = [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
        except socket.gaierror as exc:
            raise InvalidUrl(f"Could not resolve {host}.") from exc
        for address in addresses:
            try:
                if not _is_public_ip(address):
                    raise InvalidUrl("Links to private or local network addresses are not allowed.")
            except ValueError:
                continue


def looks_like_direct_file(url: str) -> bool:
    path = urlparse(url).path.lower()
    return bool(re.search(
        r"\.(zip|rar|7z|tar|gz|bz2|xz|iso|mp4|mkv|avi|mov|webm|mp3|flac|m4a|wav|pdf|epub|apk|exe|msi|dmg|bin|img|torrent)$",
        path,
    ))


def parse_aria_size(value: str) -> Optional[int]:
    """Parse aria2 sizes like 12MiB / 3.4GiB / 512B."""
    match = re.fullmatch(r"([\d.]+)\s*([KMGT]?)i?B", value.strip(), re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2).upper()
    factor = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[unit]
    return int(number * factor)


@dataclass
class CancelToken:
    """Thread-safe cancellation + deadline shared with blocking workers."""

    event: threading.Event = field(default_factory=threading.Event)
    deadline: Optional[float] = None

    def cancel(self) -> None:
        self.event.set()

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    def check(self) -> None:
        if self.event.is_set():
            raise JobCancelled("Cancelled.")
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise JobTimeout("The job hit its time limit. Try a smaller file or raise JOB_TIMEOUT.")


def safe_workdir(root: Path, name: str) -> Path:
    """Create a work directory and make sure it stays under root."""
    root = root.resolve()
    path = (root / name).resolve()
    if root not in path.parents and path != root:
        raise InvalidUrl("Invalid work directory.")
    path.mkdir(parents=True, exist_ok=True)
    return path
