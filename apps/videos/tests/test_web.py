import json
from datetime import timedelta

from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.memberships import services as memberships
from apps.videos import services
from apps.videos.models import Video, VideoCategory

from .helpers import API_KEY, DEMO, TOKEN_KEY, User, VideoTestCase, bunny_http, enable_videos, guid, make_user, \
    timezone

LIBRARY = reverse("videos:library")


class LibraryTests(VideoTestCase):
    def test_library_lists_only_published_ready_videos(self):
        shown = self.published_video("Shown clip")
        self.make_video("Draft clip")
        archived = services.archive(self.manager, self.published_video("Archived clip"))
        response = self.client.get(LIBRARY)
        self.assertContains(response, "Shown clip")
        self.assertNotContains(response, "Draft clip")
        self.assertNotContains(response, "Archived clip")
        self.assertEqual(self.client.get(reverse("videos:detail", args=[archived.slug])).status_code, 404)
        self.assertEqual(self.client.get(reverse("videos:detail", args=["draft-clip"])).status_code, 404)
        self.assertEqual(self.client.get(reverse("videos:detail", args=[shown.slug])).status_code, 200)

    def test_search_category_and_inactive_category(self):
        self.published_video("Guitar lesson")
        other = services.create_category(self.manager, name="News")
        self.published_video("Morning news", category=other)
        self.assertNotContains(self.client.get(LIBRARY, {"q": "guitar"}), "Morning news")
        page = self.client.get(reverse("videos:category", args=["news"]))
        self.assertContains(page, "Morning news")
        self.assertNotContains(page, "Guitar lesson")
        services.set_category_active(self.manager, other, False)
        self.assertEqual(self.client.get(reverse("videos:category", args=["news"])).status_code, 404)
        self.assertNotContains(self.client.get(LIBRARY), "Morning news")

    def test_no_n_plus_one(self):
        self.published_video("Clip 0")

        def count():
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(LIBRARY).status_code, 200)
            return len(queries)

        count()  # Warm per-request caches (site configuration, features).
        baseline = count()
        for index in range(1, 8):
            self.published_video(f"Clip {index}", category=services.create_category(self.manager, name=f"C{index}"))
        self.assertEqual(count(), baseline)

    def test_feature_flag_hides_everything(self):
        video = self.published_video()
        enable_videos(False)
        self.assertEqual(self.client.get(LIBRARY).status_code, 404)
        self.assertEqual(self.client.get(reverse("videos:detail", args=[video.slug])).status_code, 404)


class PlaybackTests(VideoTestCase):
    def setUp(self):
        super().setUp()
        self.video = self.published_video("Members clip")
        self.url = reverse("videos:detail", args=[self.video.slug])

    def assert_refused(self, reason, user=None):
        if user:
            self.client.force_login(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["playback_reason"], reason)
        self.assertIsNone(response.context["playback"])
        self.assertNotContains(response, "player.mediadelivery.net")
        self.assertNotContains(response, "token=")

    def test_anonymous_and_no_membership(self):
        self.assert_refused("not_authenticated")
        self.assert_refused("no_membership", self.member)
        self.assert_refused("email_unverified", make_user("unverified@example.com", verified=False))

    def test_active_member_gets_short_lived_signed_player(self):
        self.active_membership()
        self.client.force_login(self.member)
        received = []
        services.playback_authorized.connect(lambda **kw: received.append(kw), weak=False, dispatch_uid="t")
        try:
            response = self.client.get(self.url)
        finally:
            services.playback_authorized.disconnect(dispatch_uid="t")
        playback = response.context["playback"]
        self.assertEqual(playback.kind, "iframe")
        self.assertContains(response, f"https://player.mediadelivery.net/embed/12345/{self.video.provider_video_id}")
        self.assertContains(response, "token=")
        self.assertLessEqual(playback.expires_at, timezone.now() + timedelta(seconds=601))
        self.assertNotContains(response, API_KEY)
        self.assertNotContains(response, TOKEN_KEY)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["video"].pk, self.video.pk)
        # Page visits are never treated as verified viewing: nothing is audited or credited.
        self.assertFalse(AuditEvent.objects.filter(action__icontains="view").exists())

    def test_suspended_and_expired_memberships(self):
        subscription = self.active_membership()
        memberships.suspend(self.root, subscription, "ADMIN_REVIEW")
        self.assert_refused("membership_suspended", self.member)
        memberships.reinstate(self.root, subscription)
        memberships.process_expiry(now=timezone.now() + timedelta(days=400))
        self.assert_refused("no_membership")

    def test_entitlement_required(self):
        premium = self.published_video("Premium clip", required_entitlement="videos.premium")
        self.active_membership()
        self.client.force_login(self.member)
        response = self.client.get(reverse("videos:detail", args=[premium.slug]))
        self.assertEqual(response.context["playback_reason"], "entitlement_missing")


@DEMO
class DemoPlaybackTests(VideoTestCase):
    @override_settings(VIDEO_DEMO_SAMPLE_URL="")
    def test_demo_placeholder_is_labelled(self):
        video = services.publish(self.manager, self.make_video("Demo clip"))
        self.active_membership()
        self.client.force_login(self.member)
        response = self.client.get(reverse("videos:detail", args=[video.slug]))
        self.assertEqual(response.context["playback"].kind, "placeholder")
        self.assertContains(response, "Demo sample")


class DashboardTests(VideoTestCase):
    def test_member_dashboard(self):
        self.published_video("Latest clip")
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)
        self.client.force_login(self.member)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Latest clip")
        self.assertContains(response, "You do not have an active membership")
        # Phases 06-10 replaced every Coming-soon item; real money is still described as unavailable.
        self.assertContains(response, "Real rewards are not available yet")
        # Advertisements became "Sponsored videos" in Phase 06 (shown when advertisers are enabled).
        for label in ("Rewards", "Wallet", "Withdrawals"):
            self.assertContains(response, label)
        self.assertNotContains(response, "TZS 0")  # No invented balances.
        self.assertNotContains(response, reverse("administration:overview"))

    def test_content_security_policy(self):
        response = self.client.get(LIBRARY)
        policy = response["Content-Security-Policy"]
        self.assertIn("default-src 'self'", policy)
        self.assertIn("frame-src https://player.mediadelivery.net", policy)
        self.assertIn("img-src 'self' data: https://vz-test.b-cdn.net", policy)
        self.assertNotIn("unsafe-inline", policy)
        self.assertNotIn("Content-Security-Policy", self.client.get("/admin/login/"))


class ConsoleTests(VideoTestCase):
    def test_access_by_role(self):
        overview = reverse("administration:overview")
        self.assertEqual(self.client.get(overview).status_code, 302)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(overview).status_code, 302)  # Not staff: to admin login.
        for user in (self.manager, self.root):
            self.client.force_login(user)
            for name in ("overview", "videos", "categories", "video_create"):
                with self.subTest(user=user.email, page=name):
                    self.assertEqual(self.client.get(reverse(f"administration:{name}")).status_code, 200)

    def test_overview_stats_are_real(self):
        self.published_video()
        self.make_video("Draft")
        self.client.force_login(self.manager)
        sections = self.client.get(reverse("administration:overview")).context["sections"]
        self.assertEqual((sections["videos"]["publication"]["PUBLISHED"], sections["videos"]["publication"]["DRAFT"]),
                         (1, 1))
        # Phase 12: a Content Manager sees video figures only, never member counts.
        self.assertNotIn("members", sections)
        self.client.force_login(self.root)
        sections = self.client.get(reverse("administration:overview")).context["sections"]
        self.assertEqual(sections["members"]["total"], User.objects.filter(role=User.Role.MEMBER).count())

    def test_create_edit_publish_flow(self):
        self.client.force_login(self.manager)
        response = self.client.post(reverse("administration:video_create"), {
            "title": "Console clip", "description": "From the console", "category": self.category.pk,
            "required_entitlement": "videos.standard"})
        video = Video.objects.get(title="Console clip")
        self.assertRedirects(response, reverse("administration:video_detail", args=[video.pk]))
        detail = reverse("administration:video_detail", args=[video.pk])
        self.client.post(detail, {"title": "Console clip", "slug": video.slug, "description": "Edited",
                                  "category": self.category.pk, "required_entitlement": "videos.standard"})
        video.refresh_from_db()
        self.assertEqual(video.description, "Edited")
        publish = reverse("administration:video_action", args=[video.pk, "publish"])
        self.client.post(publish)
        video.refresh_from_db()
        self.assertEqual(video.publication_status, "DRAFT")  # Not processed yet: refused.
        self.make_ready(video)
        self.client.post(publish)
        video.refresh_from_db()
        self.assertEqual(video.publication_status, "PUBLISHED")
        self.assertEqual(self.client.get(publish).status_code, 405)

    def test_members_cannot_use_console_actions(self):
        video = self.make_ready(self.make_video())
        self.client.force_login(self.member)
        response = self.client.post(reverse("administration:video_action", args=[video.pk, "publish"]))
        self.assertEqual(response.status_code, 302)
        video.refresh_from_db()
        self.assertEqual(video.publication_status, "DRAFT")
        self.client.force_login(self.manager)
        response = self.client.post(reverse("administration:video_action", args=[video.pk, "delete-media"]),
                                    {"confirm": "yes"})
        self.assertEqual(response.status_code, 403)  # Super Administrator only.

    def test_permanent_actions_require_confirmation(self):
        video = self.published_video()
        self.client.force_login(self.manager)
        archive = reverse("administration:video_action", args=[video.pk, "archive"])
        self.client.post(archive)
        video.refresh_from_db()
        self.assertEqual(video.publication_status, "PUBLISHED")
        self.client.post(archive, {"confirm": "yes"})
        video.refresh_from_db()
        self.assertEqual(video.publication_status, "ARCHIVED")

    def test_blank_slug_on_edit_keeps_address(self):
        video = self.published_video("Kept address")
        self.client.force_login(self.manager)
        self.client.post(reverse("administration:video_detail", args=[video.pk]), {
            "title": "Kept address", "slug": "", "description": "Edited", "category": self.category.pk,
            "required_entitlement": "videos.standard"})
        video.refresh_from_db()
        self.assertEqual((video.slug, video.description), ("kept-address", "Edited"))

    def test_list_pagination_keeps_filters(self):
        for index in range(26):
            self.make_video(f"Filtered {index}")
        self.client.force_login(self.manager)
        response = self.client.get(reverse("administration:videos"), {"publication": "DRAFT", "q": "Filtered"})
        self.assertContains(response, "publication=DRAFT&amp;q=Filtered&amp;page=2")

    def test_csrf_enforced(self):
        video = self.make_ready(self.make_video())
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.manager)
        self.assertEqual(client.post(reverse("administration:video_action", args=[video.pk, "publish"])).status_code,
                         403)
        self.assertEqual(client.post(reverse("administration:video_upload", args=[video.pk, "authorize"]),
                                     "{}", content_type="application/json").status_code, 403)

    def test_upload_endpoint_returns_signature_but_no_api_key(self):
        video = self.make_video()
        self.client.force_login(self.manager)
        url = reverse("administration:video_upload", args=[video.pk, "authorize"])
        with bunny_http({"guid": guid()}):
            response = self.client.post(url, json.dumps({"name": "a.mp4", "size": 2048, "type": "video/mp4"}),
                                        content_type="application/json")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["endpoint"], "https://video.bunnycdn.com/tusupload")
        self.assertIn("AuthorizationSignature", body["headers"])
        self.assertNotIn(API_KEY, response.content.decode())
        self.assertNotIn(TOKEN_KEY, response.content.decode())
        bad = self.client.post(url, json.dumps({"name": "a.exe", "size": 1, "type": "application/x-msdownload"}),
                               content_type="application/json")
        self.assertEqual((bad.status_code, bad.json()["code"]), (400, "file_type"))
        self.assertEqual(self.client.post(url, "not json", content_type="application/json").status_code, 400)
        finished = self.client.post(reverse("administration:video_upload", args=[video.pk, "finished"]))
        self.assertEqual(finished.json(), {"status": "processing"})

    def test_category_console(self):
        self.client.force_login(self.manager)
        self.client.post(reverse("administration:categories"), {"name": "Comedy", "description": "", "display_order": 1})
        comedy = VideoCategory.objects.get(slug="comedy")
        self.client.post(reverse("administration:category_edit", args=[comedy.pk]), {"toggle": "1"})
        comedy.refresh_from_db()
        self.assertFalse(comedy.is_active)


class SystemCheckTests(VideoTestCase):
    def test_checks(self):
        from apps.videos.checks import check_bunny_configuration, check_demo_provider, check_video_provider

        with override_settings(VIDEO_PROVIDER="vimeo"):
            self.assertIn("videos.E001", [e.id for e in check_video_provider(None)])
        with override_settings(VIDEO_PROVIDER="demo", DEBUG=False, VIDEO_DEMO_ALLOWED=False):
            self.assertIn("videos.E002", [e.id for e in check_demo_provider(None)])
        with override_settings(BUNNY_STREAM_API_KEY=""):
            self.assertEqual([e.id for e in check_bunny_configuration(None)], ["videos.W001"])
        self.assertEqual(check_video_provider(None), [])
