"""Upload destination registry."""

from __future__ import annotations

from ..config import Settings
from .base import Uploader, UploadResult
from .gofile import GofileUploader
from .pixeldrain import PixeldrainUploader
from .telegram import TelegramUploader

HOST_LABELS = {
    "gofile": "Gofile",
    "pixeldrain": "Pixeldrain",
    "telegram": "Telegram chat",
}


def get_uploader(
    name: str,
    settings: Settings,
    chat_id: int | None = None,
) -> Uploader:
    if name == "gofile":
        return GofileUploader(settings.gofile_token)
    if name == "pixeldrain":
        return PixeldrainUploader(settings.pixeldrain_api_key)
    if name == "telegram":
        return TelegramUploader(
            settings.telegram_token,
            chat_id,
            max_upload=settings.telegram_upload_limit,
            part_size=settings.telegram_part_size,
        )
    raise ValueError(f"Unknown upload host: {name}")


def host_status(settings: Settings) -> dict[str, tuple[bool, str]]:
    return {name: get_uploader(name, settings).available() for name in HOST_LABELS}


__all__ = ["Uploader", "UploadResult", "get_uploader", "host_status", "HOST_LABELS"]
