"""Upload destination registry."""

from __future__ import annotations

from ..config import Settings
from .base import Uploader, UploadResult
from .gofile import GofileUploader
from .pixeldrain import PixeldrainUploader

HOST_LABELS = {"gofile": "Gofile", "pixeldrain": "Pixeldrain"}


def get_uploader(name: str, settings: Settings) -> Uploader:
    if name == "gofile":
        return GofileUploader(settings.gofile_token)
    if name == "pixeldrain":
        return PixeldrainUploader(settings.pixeldrain_api_key)
    raise ValueError(f"Unknown upload host: {name}")


def host_status(settings: Settings) -> dict[str, tuple[bool, str]]:
    return {name: get_uploader(name, settings).available() for name in HOST_LABELS}


__all__ = ["Uploader", "UploadResult", "get_uploader", "host_status", "HOST_LABELS"]
