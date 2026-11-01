from dataclasses import dataclass, field
from datetime import datetime


class ProviderError(Exception):
    """Base class. Messages are safe to store: never credentials or tokens."""


class ProviderTemporaryError(ProviderError):
    """Timeouts, 5xx, rate limits: retry later."""


class ProviderPermanentError(ProviderError):
    """Rejected request (4xx) or impossible operation: do not retry."""


class ProviderNotConfigured(ProviderPermanentError):
    pass


@dataclass(frozen=True)
class ProviderVideoInfo:
    processing: str  # One of Video.Processing values
    status_code: int | None = None
    duration_seconds: int | None = None
    thumbnail_file: str = ""
    error: str = ""


@dataclass(frozen=True)
class UploadAuthorization:
    """Everything the browser needs for a direct TUS upload; no permanent secret."""

    endpoint: str
    headers: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)  # TUS Upload-Metadata values
    expires_at: datetime | None = None


@dataclass(frozen=True)
class Playback:
    """kind: "iframe" (signed provider player), "video" (demo sample file) or
    "placeholder" (demo without media)."""

    kind: str
    url: str = ""
    expires_at: datetime | None = None
