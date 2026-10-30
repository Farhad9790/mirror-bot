"""Configuration loaded from environment variables / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .utils import parse_size

load_dotenv()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_ids(name: str) -> frozenset[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return frozenset()
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return frozenset(ids)


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)
    default_host: str = "gofile"
    max_file_size: int = 8 * 1024**3
    max_concurrent_jobs: int = 2
    job_timeout: int = 2 * 60 * 60
    run_seconds: int = 0
    download_dir: Path = Path("downloads")
    state_file: Path = Path(".state/telegram.json")
    gofile_token: str | None = None
    pixeldrain_api_key: str | None = None
    telegram_upload_limit: int = 45 * 1024**2
    telegram_part_size: int = 45 * 1024**2
    cookies_file: Path | None = None
    multi_file_mode: str = "largest"
    ytdlp_format: str = "bv*+ba/b"
    aria2c_path: str = "aria2c"
    allow_private_urls: bool = False
    log_level: str = "INFO"


def load_settings() -> Settings:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather and put the token "
            "in your environment or .env file."
        )

    default_host = os.getenv("DEFAULT_HOST", "gofile").strip().lower()
    if default_host not in {"gofile", "pixeldrain"}:
        default_host = "gofile"

    multi_file_mode = os.getenv("MULTI_FILE_MODE", "largest").strip().lower()
    if multi_file_mode not in {"largest", "zip"}:
        multi_file_mode = "largest"

    cookies = os.getenv("COOKIES_FILE", "").strip()
    download_dir = Path(os.getenv("DOWNLOAD_DIR", "downloads")).expanduser()
    state_file = Path(os.getenv("STATE_FILE", ".state/telegram.json")).expanduser()

    return Settings(
        telegram_token=token,
        allowed_user_ids=_env_ids("ALLOWED_USER_IDS"),
        default_host=default_host,
        max_file_size=parse_size(os.getenv("MAX_FILE_SIZE"), 8 * 1024**3),
        max_concurrent_jobs=max(1, _env_int("MAX_CONCURRENT_JOBS", 2)),
        job_timeout=max(60, _env_int("JOB_TIMEOUT", 2 * 60 * 60)),
        run_seconds=max(0, _env_int("BOT_RUN_SECONDS", 0)),
        download_dir=download_dir,
        state_file=state_file,
        gofile_token=os.getenv("GOFILE_TOKEN", "").strip() or None,
        pixeldrain_api_key=os.getenv("PIXELDRAIN_API_KEY", "").strip() or None,
        telegram_upload_limit=parse_size(
            os.getenv("TELEGRAM_UPLOAD_LIMIT"), 45 * 1024**2
        ),
        telegram_part_size=parse_size(
            os.getenv("TELEGRAM_PART_SIZE"), 45 * 1024**2
        ),
        cookies_file=Path(cookies).expanduser() if cookies else None,
        multi_file_mode=multi_file_mode,
        ytdlp_format=os.getenv("YTDLP_FORMAT", "bv*+ba/b").strip() or "bv*+ba/b",
        aria2c_path=os.getenv("ARIA2C_PATH", "aria2c").strip() or "aria2c",
        allow_private_urls=_env_bool("ALLOW_PRIVATE_URLS", False),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )
