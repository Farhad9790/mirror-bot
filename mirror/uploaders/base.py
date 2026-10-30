"""Upload destination interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..utils import CancelToken

ProgressCB = Callable[[Optional[float], str], None]


@dataclass(slots=True)
class UploadResult:
    url: str
    host: str
    detail: str = ""


class Uploader(ABC):
    name: str = ""

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """Return (usable, reason_when_unusable)."""

    @abstractmethod
    def upload(self, path: Path, progress: ProgressCB, cancel: CancelToken) -> UploadResult:
        """Upload one file and return its public link."""
