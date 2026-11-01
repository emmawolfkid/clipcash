from django.conf import settings

from .base import (  # noqa: F401
    Playback,
    ProviderError,
    ProviderNotConfigured,
    ProviderPermanentError,
    ProviderTemporaryError,
    ProviderVideoInfo,
    UploadAuthorization,
)
from .bunny import BunnyStreamProvider
from .demo import DemoProvider


def demo_allowed():
    return bool(settings.DEBUG or settings.VIDEO_DEMO_ALLOWED)


def default_provider_code():
    return "DEMO" if settings.VIDEO_PROVIDER == "demo" else "BUNNY"


def get_provider(code=None):
    """Provider for a video's stored provider code (default: the configured one)."""
    code = code or default_provider_code()
    if code == "DEMO":
        if not demo_allowed():
            raise ProviderNotConfigured("Demo videos are disabled in this environment.")
        return DemoProvider(sample_url=settings.VIDEO_DEMO_SAMPLE_URL)
    if code == "BUNNY":
        return BunnyStreamProvider(
            library_id=settings.BUNNY_STREAM_LIBRARY_ID,
            api_key=settings.BUNNY_STREAM_API_KEY,
            cdn_hostname=settings.BUNNY_STREAM_CDN_HOSTNAME,
            token_key=settings.BUNNY_STREAM_TOKEN_SECURITY_KEY,
            api_base=settings.BUNNY_STREAM_API_BASE,
            embed_base=settings.BUNNY_STREAM_EMBED_BASE,
            timeout=settings.BUNNY_STREAM_TIMEOUT_SECONDS,
        )
    raise ProviderNotConfigured(f"Unknown video provider {code!r}.")
