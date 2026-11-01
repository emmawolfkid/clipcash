from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.test import override_settings

from apps.accounts.tests.helpers import run_worker
from apps.audit.models import AuditEvent
from apps.core.models import Job
from apps.videos import services
from apps.videos.models import Video, VideoCategory

from .helpers import API_KEY, DEMO, TOKEN_KEY, User, VideoTestCase, bunny_http, guid, make_user, timezone

P = Video.Processing
PUB = Video.Publication


def actions():
    return list(AuditEvent.objects.order_by("occurred_at").values_list("action", flat=True))


class CategoryTests(VideoTestCase):
    def test_create_update_and_toggle_are_audited(self):
        self.assertEqual(self.category.slug, "music")
        services.update_category(self.manager, self.category, {"description": "All music", "display_order": 2})
        services.set_category_active(self.manager, self.category, False)
        self.category.refresh_from_db()
        self.assertFalse(self.category.is_active)
        self.assertEqual(self.category.display_order, 2)
        for action in ("video.category.created", "video.category.updated", "video.category.status_changed"):
            self.assertIn(action, actions())

    def test_names_unique_case_insensitively(self):
        with self.assertRaises(ValidationError):
            services.create_category(self.manager, name="MUSIC", slug="music-2")

    def test_members_cannot_manage_categories(self):
        with self.assertRaises(services.NotAuthorized):
            services.create_category(self.member, name="Hack")
        denied = AuditEvent.objects.get(action="video.action.denied")
        self.assertEqual(denied.outcome, "DENIED")


class VideoLifecycleTests(VideoTestCase):
    def test_new_bunny_video_is_a_pending_draft(self):
        video = self.make_video(duration_seconds=500)
        self.assertEqual((video.provider, video.processing_status, video.publication_status),
                         ("BUNNY", P.PENDING, PUB.DRAFT))
        self.assertIsNone(video.duration_seconds)  # Only Bunny reports durations.
        self.assertEqual(video.provider_video_id, "")
        self.assertIn("video.created", actions())

    def test_inactive_category_rejected(self):
        self.category = services.set_category_active(self.manager, self.category, False)
        with self.assertRaises(ValidationError):
            self.make_video()

    def test_publication_rules(self):
        video = self.make_video()
        with self.assertRaises(services.InvalidState) as caught:
            services.publish(self.manager, video)
        self.assertEqual(caught.exception.code, "not_ready")
        video = self.make_ready(video)
        video = services.publish(self.manager, video)
        self.assertEqual(video.publication_status, PUB.PUBLISHED)
        self.assertIsNotNone(video.published_at)
        with self.assertRaises(services.InvalidState):
            services.publish(self.manager, video)
        with self.assertRaises(ValidationError):
            services.update_video(self.manager, video, {"slug": "new-address"})
        video = services.unpublish(self.manager, video)
        self.assertEqual(video.publication_status, PUB.DRAFT)
        video = services.archive(self.manager, video)
        self.assertEqual(video.publication_status, PUB.ARCHIVED)
        for operation in (services.publish, services.archive):
            with self.assertRaises(services.InvalidState):
                operation(self.manager, video)
        with self.assertRaises(services.InvalidState):
            services.update_video(self.manager, video, {"title": "Changed"})
        publication = {"video.published", "video.unpublished", "video.archived"}
        self.assertEqual([a for a in actions() if a in publication],
                         ["video.published", "video.unpublished", "video.archived"])

    def test_cannot_publish_into_inactive_category(self):
        video = self.make_ready(self.make_video())
        self.category = services.set_category_active(self.manager, self.category, False)
        with self.assertRaises(services.InvalidState) as caught:
            services.publish(self.manager, video)
        self.assertEqual(caught.exception.code, "inactive_category")

    def test_members_and_plain_staff_cannot_publish(self):
        video = self.make_ready(self.make_video())
        for actor in (self.member, None):
            with self.subTest(actor=actor), self.assertRaises(services.NotAuthorized):
                services.publish(actor, video)
        video.refresh_from_db()
        self.assertEqual(video.publication_status, PUB.DRAFT)


class UploadTests(VideoTestCase):
    def test_prepare_creates_remote_video_once_and_signs(self):
        video = self.make_video()
        remote = guid()
        with bunny_http({"guid": remote}) as http:
            auth = services.prepare_upload(self.manager, video, filename="a.mp4", size=10 * 1024 * 1024,
                                           content_type="video/mp4")
        self.assertEqual(http.call_count, 1)
        video.refresh_from_db()
        self.assertEqual((video.provider_video_id, video.provider_library_id, video.processing_status),
                         (remote, "12345", P.UPLOADING))
        self.assertEqual(auth.headers["VideoId"], remote)
        # Second authorization re-uses the remote video: no new HTTP call.
        with bunny_http() as http:
            services.prepare_upload(self.manager, video, filename="a.mp4", size=1024, content_type="video/mp4")
        self.assertEqual(http.call_count, 0)
        event = AuditEvent.objects.filter(action="video.upload.initiated").order_by("occurred_at").first()
        self.assertEqual(event.metadata, {"content_type": "video/mp4", "size_mb": 10})

    def test_validation(self):
        video = self.make_video()
        for kwargs in ({"content_type": "application/x-msdownload", "size": 10},
                       {"content_type": "video/mp4", "size": 0},
                       {"content_type": "video/mp4", "size": "10"},
                       {"content_type": "video/mp4", "size": True},
                       {"content_type": "video/mp4", "size": 5000 * 1024 * 1024}):
            with self.subTest(kwargs=kwargs), bunny_http(), self.assertRaises(services.UploadRejected):
                services.prepare_upload(self.manager, video, filename="x", **kwargs)
        with self.assertRaises(services.NotAuthorized):
            services.prepare_upload(self.member, video, filename="x", size=1, content_type="video/mp4")

    def test_provider_failure_releases_claim(self):
        video = self.make_video()
        with bunny_http(503), self.assertRaises(services.ProviderUnavailable):
            services.prepare_upload(self.manager, video, filename="a", size=1, content_type="video/mp4")
        video.refresh_from_db()
        self.assertIsNone(video.remote_creation_started_at)
        self.assertEqual((video.provider_video_id, video.processing_status), ("", P.PENDING))

    def test_concurrent_claim_is_refused(self):
        video = self.make_video()
        Video.objects.filter(pk=video.pk).update(remote_creation_started_at=timezone.now())
        with bunny_http(), self.assertRaises(services.InvalidState) as caught:
            services.prepare_upload(self.manager, video, filename="a", size=1, content_type="video/mp4")
        self.assertEqual(caught.exception.code, "in_progress")
        # A stale claim (crashed request) can be taken over.
        Video.objects.filter(pk=video.pk).update(remote_creation_started_at=timezone.now() - timedelta(minutes=10))
        with bunny_http({"guid": guid()}):
            services.prepare_upload(self.manager, video, filename="a", size=1, content_type="video/mp4")

    def test_finished_queues_sync_and_job_marks_ready(self):
        video = self.make_video()
        with bunny_http({"guid": guid()}):
            services.prepare_upload(self.manager, video, filename="a", size=1, content_type="video/mp4")
        services.report_upload_finished(self.manager, video)
        services.report_upload_finished(self.manager, video)  # Idempotent.
        video.refresh_from_db()
        self.assertEqual(video.processing_status, P.PROCESSING)
        self.assertEqual(Job.objects.filter(job_type="videos.sync_status").count(), 1)
        with bunny_http({"status": 4, "length": 125.4, "thumbnailFileName": "thumbnail.jpg"}):
            run_worker()
        video.refresh_from_db()
        self.assertEqual((video.processing_status, video.duration_seconds, video.thumbnail_file),
                         (P.READY, 125, "thumbnail.jpg"))
        self.assertIsNotNone(video.last_synced_at)
        self.assertEqual(Job.objects.get(job_type="videos.sync_status").status, Job.Status.SUCCEEDED)
        self.assertIn("video.processing.completed", actions())

    def test_sync_retries_while_processing_and_records_failure(self):
        video = self.make_video()
        with bunny_http({"guid": guid()}):
            services.prepare_upload(self.manager, video, filename="a", size=1, content_type="video/mp4")
        services.report_upload_finished(self.manager, video)
        with bunny_http({"status": 3}):
            run_worker()
        job = Job.objects.get(job_type="videos.sync_status")
        self.assertEqual(job.status, Job.Status.RETRY_WAIT)
        Job.objects.filter(pk=job.pk).update(run_after=timezone.now())
        with bunny_http(500):
            run_worker()
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts), (Job.Status.RETRY_WAIT, 2))
        Job.objects.filter(pk=job.pk).update(run_after=timezone.now())
        with bunny_http({"status": 5}):
            run_worker()
        video.refresh_from_db()
        self.assertEqual(video.processing_status, P.FAILED)
        self.assertIn("encoding error", video.processing_error)
        self.assertIn("video.processing.failed", actions())

    def test_sync_never_downgrades_a_published_video(self):
        video = self.published_video()
        services.enqueue_sync(video)
        with bunny_http({"status": 5, "length": 99, "thumbnailFileName": "new.jpg"}):
            run_worker()
        video.refresh_from_db()
        self.assertEqual((video.processing_status, video.publication_status), (P.READY, PUB.PUBLISHED))
        self.assertEqual(video.thumbnail_file, "new.jpg")
        self.assertEqual(Job.objects.get(job_type="videos.sync_status").status, Job.Status.SUCCEEDED)
        self.assertNotIn("video.processing.failed", actions())

    def test_permanent_provider_error_fails_job_and_audits(self):
        video = self.make_video()
        with bunny_http({"guid": guid()}):
            services.prepare_upload(self.manager, video, filename="a", size=1, content_type="video/mp4")
        services.report_upload_finished(self.manager, video)
        with bunny_http(401):
            run_worker()
        self.assertEqual(Job.objects.get(job_type="videos.sync_status").status, Job.Status.FAILED)
        event = AuditEvent.objects.get(action="video.provider.sync_failed")
        self.assertEqual(event.metadata["job_type"], "videos.sync_status")

    def test_failed_upload_and_stale_sweep(self):
        video = self.make_video()
        with bunny_http({"guid": guid()}):
            services.prepare_upload(self.manager, video, filename="a", size=1, content_type="video/mp4")
        services.report_upload_failed(self.manager, video, "<script>")
        video.refresh_from_db()
        self.assertEqual(video.processing_status, P.FAILED)
        self.assertEqual(AuditEvent.objects.get(action="video.upload.failed").metadata, {"reason": "unknown"})

        other = self.make_video("Stuck")
        with bunny_http({"guid": guid()}):
            services.prepare_upload(self.manager, other, filename="a", size=1, content_type="video/mp4")
        Video.objects.filter(pk=other.pk).update(updated_at=timezone.now() - timedelta(hours=30))
        self.assertEqual(services.sweep_unfinished(), 0)
        other.refresh_from_db()
        self.assertEqual(other.processing_status, P.FAILED)

    def test_media_deletion_super_admin_only(self):
        video = self.make_ready(self.make_video())
        with self.assertRaises(services.InvalidState):
            services.request_media_deletion(self.root, video)
        video = services.archive(self.manager, video)
        with self.assertRaises(services.NotAuthorized):
            services.request_media_deletion(self.manager, video)
        services.request_media_deletion(self.root, video)
        with bunny_http({}) as http:
            run_worker()
        self.assertEqual(http.call_args.args[0].get_method(), "DELETE")
        video.refresh_from_db()
        self.assertEqual(video.processing_status, P.FAILED)
        self.assertIn("video.media.deleted", actions())

    def test_audit_never_contains_credentials(self):
        video = self.make_video()
        with bunny_http({"guid": guid()}):
            services.prepare_upload(self.manager, video, filename="a", size=1, content_type="video/mp4")
        for event in AuditEvent.objects.all():
            text = str(event.metadata) + event.target_repr
            self.assertNotIn(API_KEY, text)
            self.assertNotIn(TOKEN_KEY, text)
            self.assertNotIn("AuthorizationSignature", text)


@DEMO
@override_settings(DEBUG=False)
class DemoModeTests(VideoTestCase):
    def test_demo_videos_ready_labelled_and_without_uploads(self):
        video = self.make_video("Sample", duration_seconds=42)
        self.assertEqual((video.provider, video.processing_status, video.duration_seconds), ("DEMO", P.READY, 42))
        self.assertTrue(video.provider_video_id.startswith("demo-"))
        with self.assertRaises(services.UploadRejected):
            services.prepare_upload(self.manager, video, filename="a", size=1, content_type="video/mp4")
        services.publish(self.manager, video)
        with override_settings(VIDEO_DEMO_ALLOWED=False):
            self.assertFalse(services.library_queryset().exists())
            with self.assertRaises(services.ProviderUnavailable):
                self.make_video("Another")


class DatabaseGuardTests(VideoTestCase):
    def test_published_requires_ready(self):
        video = self.published_video()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Video.objects.filter(pk=video.pk).update(processing_status=P.PROCESSING)

    def test_no_deletes_and_archive_is_terminal(self):
        video = self.make_ready(self.make_video())
        with self.assertRaises(DatabaseError), transaction.atomic():
            Video.objects.filter(pk=video.pk).delete()
        with self.assertRaises(DatabaseError), transaction.atomic():
            VideoCategory.objects.filter(pk=self.category.pk).delete()
        services.archive(self.manager, video)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Video.objects.filter(pk=video.pk).update(publication_status=PUB.DRAFT)

    def test_provider_id_immutable_and_unique(self):
        video = self.make_ready(self.make_video())
        with self.assertRaises(DatabaseError), transaction.atomic():
            Video.objects.filter(pk=video.pk).update(provider_video_id=guid())
        other = self.make_video("Second")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Video.objects.filter(pk=other.pk).update(provider_video_id=video.provider_video_id)


class RolePermissionTests(VideoTestCase):
    def test_content_manager_permissions(self):
        self.assertTrue(services.can_manage_videos(self.manager))
        self.assertTrue(services.can_publish_videos(self.manager))
        self.assertFalse(services.can_use_video_console(self.member))
        self.assertTrue(services.can_use_video_console(self.root))
        suspended = make_user("cm2@example.com", role=User.Role.CONTENT_MANAGER, status=User.Status.SUSPENDED)
        self.assertFalse(services.can_manage_videos(suspended))
