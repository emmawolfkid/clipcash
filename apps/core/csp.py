"""
Content-Security-Policy for ClipCash pages (not the Django admin, which uses
inline scripts). Only the site itself, the Bunny player (frames), Bunny's TUS
upload endpoint (connections) and the Bunny CDN (thumbnails) are allowed.
"""

from urllib.parse import urlsplit

from django.conf import settings


def _policy():
    cdn = settings.BUNNY_STREAM_CDN_HOSTNAME.strip().removeprefix("https://").rstrip("/")
    images = "'self' data:" + (f" https://{cdn}" if cdn else "")
    media = "'self'"
    sample = urlsplit(settings.VIDEO_DEMO_SAMPLE_URL or "")
    if sample.scheme == "https" and sample.netloc:
        media += f" https://{sample.netloc}"
    return "; ".join([
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        f"img-src {images}",
        f"media-src {media}",
        "frame-src https://player.mediadelivery.net https://iframe.mediadelivery.net",
        "connect-src 'self' https://video.bunnycdn.com",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])


class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not request.path.startswith("/admin/") and "Content-Security-Policy" not in response:
            response["Content-Security-Policy"] = _policy()
        return response
