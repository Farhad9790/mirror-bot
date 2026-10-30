"""User-facing error types used across the bot."""

from __future__ import annotations


class MirrorError(Exception):
    """Base class for errors that are safe to show to a Telegram user."""


class InvalidUrl(MirrorError):
    """The supplied URL is missing, malformed or not allowed."""


class ResolutionError(MirrorError):
    """A page did not expose a downloadable file."""


class DownloadError(MirrorError):
    """Downloading the remote file failed."""


class UploadError(MirrorError):
    """Uploading to the destination failed."""


class UploaderUnavailable(MirrorError):
    """The chosen destination is not configured."""


class JobCancelled(MirrorError):
    """The user cancelled the job."""


class JobTimeout(MirrorError):
    """The job exceeded its time budget."""


class FileTooLarge(MirrorError):
    """The file is bigger than the configured limit."""
