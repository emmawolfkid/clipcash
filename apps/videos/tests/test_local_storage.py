"""OPS-01: local video storage — upload validation and limits, MP4 duration without FFmpeg, protected
streaming (signed, viewer-bound, re-checked per request, HTTP Range), thumbnails, archived-file deletion,
local copies of other videos, storage reporting and playback in the member library."""

import shutil
import struct
import tempfile
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.videos import local_storage, services
from apps.videos.models import Video
from apps.videos.providers.local import mp4_duration_seconds

from .helpers import User, VideoTestCase, make_user

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def mp4_bytes(seconds=95, payload=4000):
    """A minimal ISO media file: ftyp + moov/mvhd (version 0, timescale 1000) + mdat."""
    ftyp = struct.pack(">I4s4sI8s", 24, b"ftyp", b"isom", 512, b"isomiso2")
    body = struct.pack(">B3xIIII", 0, 0, 0, 1000, seconds * 1000) + b"\x00" * 80
    mvhd = struct.pack(">I4s", 8 + len(body), b"mvhd") + body
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    mdat = struct.pack(">I4s", 8 + payload, b"mdat") + bytes(range(256)) * (payload // 256) + b"\x00" * (payload % 256)
    return ftyp + moov + mdat


class LocalStorageTestCase(VideoTestCase):
    def setUp(self):
        super().setUp()
        self.storage = tempfile.mkdtemp(prefix="cc-videos-")
        self.addCleanup(shutil.rmtree, self.storage, True)
        override = override_settings(VIDEO_PROVIDER="local", VIDEO_LOCAL_ROOT=self.storage, VIDEO_LOCAL_MAX_FILE_MB=2,
                                     VIDEO_LOCAL_TOTAL_MB=3, VIDEO_LOCAL_SENDFILE_HEADER="")
        override.enable()
        self.addCleanup(override.disable)

    def upload(self, video=None, data=None, name="clip.mp4", thumbnail=None, duration=None):
        self.counter = getattr(self, "counter", 0) + 1
        video = video or self.make_video(f"Local clip {self.counter}")
        media = SimpleUploadedFile(name, mp4_bytes() if data is None else data, content_type="video/mp4")
        thumb = SimpleUploadedFile("t.png", thumbnail, content_type="image/png") if thumbnail else None
        return local_storage.store_upload(self.manager, video, media, thumbnail=thumb, reported_duration=duration)

    def published_local(self, **kwargs):
        video = self.upload(**kwargs)
        return services.publish(self.manager, video)


class UploadTests(LocalStorageTestCase):
    def test_upload_stores_a_private_file_and_reads_the_duration(self):
        video = self.upload(thumbnail=PNG)
        self.assertEqual((video.provider, video.processing_status, video.duration_seconds), ("LOCAL", "READY", 95))
        path = Path(self.storage, "media", f"{video.provider_video_id}.mp4")
        self.assertTrue(path.is_file())
        self.assertRegex(video.provider_video_id, r"^[0-9a-f]{32}$")  # Generated, never the uploaded name.
        self.assertTrue(Path(self.storage, "thumbnails", video.thumbnail_file).is_file())
        event = AuditEvent.objects.get(action="video.local.stored")
        self.assertEqual(event.metadata["duration_seconds"], 95)
        self.assertNotIn("clip.mp4", str(event.metadata))

    def test_only_mp4_within_limits_and_never_twice(self):
        with self.assertRaises(local_storage.LocalStorageError) as error:
            self.upload(data=b"RIFF....WEBPVP8 not a video at all")
        self.assertEqual(error.exception.code, "file_type")
        with self.assertRaises(local_storage.LocalStorageError) as error:
            self.upload(data=mp4_bytes(payload=3 * 1024 * 1024))
        self.assertEqual(error.exception.code, "file_size")
        with self.assertRaises(local_storage.LocalStorageError) as error:
            self.upload(thumbnail=b"GIF89a not allowed")
        self.assertEqual(error.exception.code, "thumbnail_type")
        video = self.upload()
        with self.assertRaises(local_storage.LocalStorageError) as error:
            self.upload(video=video)
        self.assertEqual(error.exception.code, "has_media")
        self.assertEqual(len(list(Path(self.storage, "media").iterdir())), 1)  # Refused files never stay behind.

    def test_total_storage_limit(self):
        self.upload(data=mp4_bytes(payload=1_900_000))
        with self.assertRaises(local_storage.LocalStorageError) as error:
            self.upload(video=self.make_video("Second"), data=mp4_bytes(payload=1_900_000))
        self.assertEqual(error.exception.code, "storage_full")

    def test_browser_duration_is_used_only_when_the_header_has_none(self):
        data = mp4_bytes()
        broken = data.replace(b"mvhd", b"xxxx")
        video = self.upload(data=broken, duration="61.4")
        self.assertEqual(video.duration_seconds, 61)
        self.assertIsNone(mp4_duration_seconds(Path(self.storage, "media", f"{video.provider_video_id}.mp4")))

    def test_staff_without_permission_and_archived_videos_are_refused(self):
        member = make_user("nobody@example.com")
        with self.assertRaises(services.NotAuthorized):
            local_storage.store_upload(member, self.make_video("X"), SimpleUploadedFile("a.mp4", mp4_bytes()))
        video = services.archive(self.manager, self.make_video("Archived"))
        with self.assertRaises(local_storage.LocalStorageError):
            self.upload(video=video)


class StreamingTests(LocalStorageTestCase):
    def setUp(self):
        super().setUp()
        self.active_membership()
        self.video = self.published_local(thumbnail=PNG)

    def playback_url(self, user=None):
        reason, playback = services.authorize_playback(user or self.member, self.video)
        self.assertEqual(reason, "ok")
        self.assertEqual(playback.kind, "video")
        return playback.url

    def test_member_with_access_streams_and_seeks(self):
        url = self.playback_url()
        self.client.force_login(self.member)
        full = self.client.get(url)
        self.assertEqual(full.status_code, 200)
        body = b"".join(full.streaming_content)
        self.assertEqual(body, mp4_bytes())
        self.assertEqual(full["Accept-Ranges"], "bytes")
        self.assertIn("private", full["Cache-Control"])
        part = self.client.get(url, HTTP_RANGE="bytes=100-199")
        self.assertEqual(part.status_code, 206)
        self.assertEqual(part["Content-Range"], f"bytes 100-199/{len(mp4_bytes())}")
        self.assertEqual(b"".join(part.streaming_content), mp4_bytes()[100:200])
        tail = self.client.get(url, HTTP_RANGE="bytes=-10")
        self.assertEqual(b"".join(tail.streaming_content), mp4_bytes()[-10:])
        self.assertEqual(self.client.get(url, HTTP_RANGE="bytes=999999-").status_code, 416)
        self.assertEqual(self.client.get(url, HTTP_RANGE="items=1-2").status_code, 416)

    def test_access_is_refused_without_a_valid_viewer_token_or_entitlement(self):
        url = self.playback_url()
        self.assertEqual(self.client.get(url).status_code, 403)  # Signed out.
        stranger = make_user("stranger@example.com")
        self.active_membership(member=stranger)
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(url).status_code, 403)  # A link copied to another account.
        bare = reverse("videos:media", args=[self.video.provider_video_id])
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(bare).status_code, 403)  # No token.
        self.assertEqual(self.client.get(bare + "?t=forged").status_code, 403)
        self.assertEqual(self.client.get("/videos/media/..%2F..%2Fetc%2Fpasswd/").status_code, 404)
        # Membership ends: the same link stops working at once.
        from apps.memberships import services as memberships

        for subscription in self.member.subscriptions.filter(status="ACTIVE"):
            memberships.cancel(self.root, subscription, "ADMIN_REVIEW")
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertTrue(AuditEvent.objects.filter(action="video.media.access_denied").exists())

    def test_unpublished_videos_stream_to_staff_only(self):
        url = self.playback_url()
        services.unpublish(self.manager, self.video)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(url).status_code, 403)
        preview = services.staff_preview(self.manager, Video.objects.get(pk=self.video.pk))
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(preview.url).status_code, 200)

    def test_thumbnail_and_library_page(self):
        thumb = services.thumbnail_url(self.video)
        self.client.force_login(self.member)
        response = self.client.get(thumb)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        page = self.client.get(reverse("videos:detail", args=[self.video.slug]))
        self.assertContains(page, "/videos/media/" + self.video.provider_video_id)
        self.assertContains(page, "<video")

    @override_settings(VIDEO_LOCAL_SENDFILE_HEADER="X-Sendfile")
    def test_web_server_hand_off_when_configured(self):
        url = self.playback_url()
        self.client.force_login(self.member)
        response = self.client.get(url)
        self.assertTrue(response["X-Sendfile"].endswith(f"{self.video.provider_video_id}.mp4"))
        self.assertEqual(response.content, b"")


class FileManagementTests(LocalStorageTestCase):
    def test_archived_file_deletion_keeps_the_record(self):
        root = User.objects.filter(role=User.Role.SUPER_ADMIN).first()
        video = self.published_local()
        with self.assertRaises(services.InvalidState):
            services.request_media_deletion(root, video)  # Published: archive first.
        video = services.archive(self.manager, video)
        services.request_media_deletion(root, video)
        video.refresh_from_db()
        self.assertEqual((video.processing_status, video.publication_status), ("FAILED", "ARCHIVED"))
        self.assertFalse(Path(self.storage, "media", f"{video.provider_video_id}.mp4").exists())
        self.assertTrue(Video.objects.filter(pk=video.pk).exists())

    def test_local_copy_of_another_provider(self):
        with override_settings(VIDEO_PROVIDER="bunny"):
            bunny = self.published_video("Bunny clip")
        copy = local_storage.create_local_copy(self.manager, bunny)
        self.assertEqual((copy.provider, copy.title, copy.required_entitlement, copy.publication_status),
                         ("LOCAL", "Bunny clip", bunny.required_entitlement, "DRAFT"))
        bunny.refresh_from_db()
        self.assertEqual((bunny.provider, bunny.publication_status), ("BUNNY", "PUBLISHED"))  # Untouched.
        self.assertEqual(self.upload(video=copy).processing_status, "READY")

    def test_storage_summary_and_console_pages(self):
        video = self.upload()
        summary = local_storage.storage_summary()
        self.assertEqual(summary["used"], len(mp4_bytes()))
        self.assertEqual(summary["videos"][0]["size"], len(mp4_bytes()))
        self.client.force_login(self.manager)
        page = self.client.get(reverse("administration:video_storage"))
        self.assertContains(page, video.title)
        self.assertNotContains(page, self.storage)  # Folder shown to Super Administrators only.
        detail = self.client.get(reverse("administration:video_detail", args=[video.pk]))
        self.assertContains(detail, "/videos/media/")  # Staff preview.

    def test_console_one_step_upload(self):
        self.client.force_login(self.manager)
        response = self.client.post(reverse("administration:video_create"), {
            "title": "Owner upload", "required_entitlement": "videos.standard", "category": self.category.pk,
            "media": SimpleUploadedFile("owner.mp4", mp4_bytes(), content_type="video/mp4")})
        video = Video.objects.get(title="Owner upload")
        self.assertRedirects(response, reverse("administration:video_detail", args=[video.pk]))
        self.assertEqual((video.provider, video.processing_status, video.duration_seconds), ("LOCAL", "READY", 95))
        ajax = self.client.post(reverse("administration:video_local_upload", args=[self.make_video("Later").pk]),
                                {"media": SimpleUploadedFile("later.mp4", mp4_bytes(), content_type="video/mp4")},
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertIn("redirect", ajax.json())
