from django.conf import settings
from django.core.checks import Error, Warning, register


@register()
def check_video_provider(app_configs, **kwargs):
    errors = []
    if settings.VIDEO_PROVIDER not in ("bunny", "demo"):
        errors.append(Error("VIDEO_PROVIDER must be 'bunny' or 'demo'.", id="videos.E001"))
    return errors


@register(deploy=True)
def check_demo_provider(app_configs, **kwargs):
    # Deploy-only (the test runner forces DEBUG=False). At runtime get_provider()
    # refuses demo videos outside DEBUG anyway.
    if settings.VIDEO_PROVIDER == "demo" and not (settings.DEBUG or settings.VIDEO_DEMO_ALLOWED):
        return [Error(
            "The demo video provider is disabled in production.",
            hint="Use VIDEO_PROVIDER=bunny, or set VIDEO_DEMO_ALLOWED=True only for an isolated demo deployment.",
            id="videos.E002")]
    return []


@register(deploy=True)
def check_bunny_configuration(app_configs, **kwargs):
    if settings.VIDEO_PROVIDER != "bunny":
        return []
    missing = [name for name in ("BUNNY_STREAM_LIBRARY_ID", "BUNNY_STREAM_API_KEY", "BUNNY_STREAM_CDN_HOSTNAME",
                                 "BUNNY_STREAM_TOKEN_SECURITY_KEY") if not getattr(settings, name)]
    if missing:
        return [Warning("Bunny Stream settings are missing: " + ", ".join(missing),
                        hint="Uploads and playback will fail until they are configured.", id="videos.W001")]
    return []
