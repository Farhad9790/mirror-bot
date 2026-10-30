"""Upload destination registry."""

from __future__ import annotations

from ..config import Settings
from .base import Uploader, UploadResult
from .gofile import GofileUploader


def get_uploader(name: str, settings: Settings) -> Uploader:
    if name == "gofile":
        return GofileUploader(settings.gofile_token)
    raise ValueError(f"Unknown upload host: {name}")
