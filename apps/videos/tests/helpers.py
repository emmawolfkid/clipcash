import json
import uuid
from contextlib import contextmanager
from io import BytesIO
from unittest import mock
from urllib.error import HTTPError, URLError

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone

from apps.core.models import SiteConfiguration
from apps.memberships.tests.helpers import PASSWORD, MembershipTestCase, User, make_user
from apps.videos import services
from apps.videos.models import Video

API_KEY = "test-api-key-NEVER-SHOWN"
TOKEN_KEY = "test-token-key-NEVER-SHOWN"

BUNNY = override_settings(
    VIDEO_PROVIDER="bunny", VIDEO_DEMO_ALLOWED=False, VIDEO_DEMO_SAMPLE_URL="",
    BUNNY_STREAM_LIBRARY_ID="12345", BUNNY_STREAM_API_KEY=API_KEY,
    BUNNY_STREAM_CDN_HOSTNAME="vz-test.b-cdn.net", BUNNY_STREAM_TOKEN_SECURITY_KEY=TOKEN_KEY,
    BUNNY_STREAM_API_BASE="https://video.bunnycdn.com",
)
DEMO = override_settings(VIDEO_PROVIDER="demo", VIDEO_DEMO_ALLOWED=True)


def enable_videos(enabled=True):
    SiteConfiguration.objects.update_or_create(id=1, defaults={"videos_enabled": enabled})
    cache.clear()


def guid():
    return str(uuid.uuid4())


class FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode() if payload is not None else b""

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code):
    return HTTPError("https://video.bunnycdn.com/x", code, "error", {}, BytesIO(b""))


@contextmanager
def bunny_http(*responses):
    """Mock Bunny HTTP. Each response is a dict (JSON body), an int (HTTP error) or an exception.
    Yields the mock so tests can inspect requests. No live call is ever made."""
    queue = list(responses)

    def fake_urlopen(request, timeout=None):
        assert request.full_url.startswith("https://video.bunnycdn.com/"), request.full_url
        item = queue.pop(0)
        if isinstance(item, int):
            raise http_error(item)
        if isinstance(item, BaseException):
            raise item
        return FakeResponse(item)

    with mock.patch("apps.videos.providers.bunny._OPENER.open", side_effect=fake_urlopen) as patched:
        yield patched
    assert not queue, f"unused mocked responses: {queue}"


@BUNNY
class VideoTestCase(MembershipTestCase):
    def setUp(self):
        super().setUp()
        enable_videos()
        self.manager = make_user("content@example.com", role=User.Role.CONTENT_MANAGER)
        self.category = services.create_category(self.manager, name="Music", description="Songs")

    def make_video(self, title="First clip", **values):
        values.setdefault("category", self.category)
        return services.create_video(self.manager, title=title, **values)

    def make_ready(self, video, **extra):
        """Bypass Bunny for setup: mark a video as processed with a remote id."""
        Video.objects.filter(pk=video.pk).update(
            provider_video_id=video.provider_video_id or guid(), provider_library_id="12345",
            processing_status=Video.Processing.READY, duration_seconds=95, **extra)
        video.refresh_from_db()
        return video

    def published_video(self, title="Published clip", **values):
        video = self.make_ready(self.make_video(title, **values))
        return services.publish(self.manager, video)


__all__ = ["API_KEY", "BUNNY", "DEMO", "PASSWORD", "TOKEN_KEY", "User", "VideoTestCase", "bunny_http",
           "enable_videos", "guid", "make_user", "timezone", "URLError"]
