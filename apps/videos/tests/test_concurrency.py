import threading
import time
from unittest import mock, skipUnless

from django.db import connection
from django.test import TransactionTestCase

from apps.accounts.tests.helpers import make_user
from apps.memberships.tests.test_concurrency import run_threads
from apps.videos import services
from apps.videos.models import Video

from .helpers import BUNNY, FakeResponse, User, guid


@BUNNY
@skipUnless(connection.vendor == "postgresql", "Concurrency is verified on PostgreSQL.")
class RemoteCreationConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.manager = make_user("content@example.com", role=User.Role.CONTENT_MANAGER)
        self.video = services.create_video(self.manager, title="Race")

    def test_concurrent_upload_preparation_creates_one_bunny_video(self):
        created = []
        lock = threading.Lock()

        def fake_urlopen(request, timeout=None):
            time.sleep(0.2)  # Slow Bunny: all workers overlap with the HTTP call.
            with lock:
                created.append(request.full_url)
            return FakeResponse({"guid": guid()})

        with mock.patch("apps.videos.providers.bunny._OPENER.open", side_effect=fake_urlopen):
            results = run_threads(4, lambda i: services.prepare_upload(
                self.manager, self.video, filename="a.mp4", size=1024, content_type="video/mp4"))

        self.assertEqual(len(created), 1, results)
        succeeded = [r for r in results if not isinstance(r, Exception)]
        refused = [r for r in results if isinstance(r, services.InvalidState)]
        self.assertGreaterEqual(len(succeeded), 1)
        self.assertEqual(len(succeeded) + len(refused), 4, results)
        self.video.refresh_from_db()
        self.assertTrue(self.video.provider_video_id)
        self.assertEqual(self.video.processing_status, Video.Processing.UPLOADING)
