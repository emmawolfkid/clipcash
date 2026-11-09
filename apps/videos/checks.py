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
    messages = []
    if missing:
        # A warning, not an error: the provider refuses every upload and playback until all four
        # are set (it never produces an unsigned link), so nothing is exposed meanwhile.
        messages.append(Warning("Bunny Stream settings are missing: " + ", ".join(missing),
                                hint="Uploads and playback will fail until they are configured.", id="videos.W001"))
    library = str(settings.BUNNY_STREAM_LIBRARY_ID or "").strip()
    cdn = str(settings.BUNNY_STREAM_CDN_HOSTNAME or "").strip()
    if library and not library.isdigit():
        messages.append(Error("BUNNY_STREAM_LIBRARY_ID must be the numeric library id.", id="videos.E003"))
    if cdn and ("/" in cdn.removeprefix("https://").rstrip("/") or cdn.startswith("http://")):
        messages.append(Error("BUNNY_STREAM_CDN_HOSTNAME must be a host name such as vz-xxxx.b-cdn.net.",
                              hint="No http:// scheme and no path.", id="videos.E004"))
    return messages
