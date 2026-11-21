"""PAY-LIVE-01B: the member video catalogue is for current members only.

The library, category pages, video detail pages and the dashboard's latest videos are
shown only to an eligible account with a current, non-suspended membership, and list only
the videos the plan's entitlements include. Everyone else is redirected with a reason;
no protected title, description or signed thumbnail is rendered for them.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.messages import get_messages
from django.urls import reverse

from apps.memberships import services as memberships
from apps.memberships.models import Subscription
from apps.memberships.tests.helpers import enable_memberships
from apps.videos.models import Video

from .helpers import VideoTestCase, make_user

LIBRARY = reverse("videos:library")
PLANS = reverse("memberships:plans")
LOGIN = reverse("accounts:login")
CDN = "vz-test.b-cdn.net"  # The signed-thumbnail host in the test settings.


class CatalogueAccessTests(VideoTestCase):
    def setUp(self):
        super().setUp()
        self.video = self.published_video("Protected clip", description="Secret description")
        Video.objects.filter(pk=self.video.pk).update(thumbnail_file="thumbnail.jpg")
        self.detail = reverse("videos:detail", args=[self.video.slug])
        self.category_page = reverse("videos:category", args=[self.category.slug])
        self.protected_urls = (LIBRARY, self.category_page, self.detail)

    # --- helpers ---------------------------------------------------------------------

    def messages_of(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def assert_blocked(self, reason_text, *, target=PLANS):
        for url in self.protected_urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                location = response["Location"]
                self.assertTrue(location.startswith(target), location)
                self.assertIn(reason_text, " ".join(self.messages_of(response)))
                # Nothing protected is rendered on the way out.
                self.assertNotIn(b"Protected clip", response.content)
                self.assertNotIn(CDN.encode(), response.content)

    def assert_browsable(self):
        library = self.client.get(LIBRARY)
        self.assertContains(library, "Protected clip")
        self.assertContains(library, CDN)  # Members do get signed thumbnails.
        self.assertContains(self.client.get(self.category_page), "Protected clip")
        detail = self.client.get(self.detail)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.context["playback_reason"], "ok")

    # --- scenarios -------------------------------------------------------------------

    def test_anonymous_visitors_are_sent_to_sign_in_and_back(self):
        for url in self.protected_urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertRedirects(response, f"{LOGIN}?next={url}", fetch_redirect_response=False)
                self.assertIn("Sign in to browse videos", " ".join(self.messages_of(response)))
                self.assertNotIn(b"Protected clip", response.content)

    def test_unknown_slugs_look_the_same_to_non_members(self):
        """No probing: a non-member cannot tell which videos or categories exist."""
        for url in (reverse("videos:detail", args=["no-such-video"]),
                    reverse("videos:category", args=["no-such-category"])):
            with self.subTest(url=url):
                self.assertRedirects(self.client.get(url), f"{LOGIN}?next={url}", fetch_redirect_response=False)
        self.client.force_login(self.member)
        self.assertRedirects(self.client.get(reverse("videos:detail", args=["no-such-video"])), PLANS)

    def test_signed_in_member_without_membership(self):
        self.client.force_login(self.member)
        self.assert_blocked("Videos are available to members with an active membership")

    def test_redirect_lands_on_the_plans_page_without_a_loop(self):
        self.client.force_login(self.member)
        response = self.client.get(LIBRARY, follow=True)
        self.assertEqual(response.redirect_chain, [(PLANS, 302)])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Videos are available to members with an active membership")
        self.assertContains(response, self.plan.name)  # Plans stay browsable for non-members.

    def test_pending_payment_choice_is_not_a_membership(self):
        pending = memberships.choose_plan(self.member, self.plan)
        self.assertEqual(pending.status, Subscription.Status.PENDING)
        self.client.force_login(self.member)
        self.assert_blocked("Videos are available to members with an active membership")

    def test_active_member_browses_and_plays(self):
        self.active_membership()
        self.client.force_login(self.member)
        self.assert_browsable()

    def test_expired_membership_loses_access(self):
        subscription = self.active_membership()
        self.client.force_login(self.member)
        self.assert_browsable()
        memberships.process_expiry(now=subscription.expires_at + timedelta(seconds=1))
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Subscription.Status.EXPIRED)
        self.assert_blocked("Videos are available to members with an active membership")

    def test_suspended_membership_loses_access(self):
        subscription = self.active_membership()
        memberships.suspend(self.root, subscription, "ADMIN_REVIEW")
        self.client.force_login(self.member)
        self.assert_blocked("Your membership is suspended")

    def test_cancelled_membership_loses_access(self):
        subscription = self.active_membership()
        memberships.cancel(self.root, subscription, "ADMIN_REVIEW")
        self.client.force_login(self.member)
        self.assert_blocked("Videos are available to members with an active membership")

    def test_unverified_account_is_told_what_to_do(self):
        self.client.force_login(make_user("unverified@example.com", verified=False))
        response = self.client.get(LIBRARY)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(b"Protected clip", response.content)

    def test_only_entitled_videos_are_listed_or_opened(self):
        premium = self.published_video("Premium clip", required_entitlement="videos.premium")
        self.active_membership()  # The standard plan grants videos.standard only.
        self.client.force_login(self.member)
        library = self.client.get(LIBRARY)
        self.assertContains(library, "Protected clip")
        self.assertNotContains(library, "Premium clip")
        response = self.client.get(reverse("videos:detail", args=[premium.slug]))
        self.assertRedirects(response, PLANS)
        self.assertIn("does not include this video", " ".join(self.messages_of(response)))

    def test_premium_plan_member_sees_premium_videos(self):
        self.published_video("Premium clip", required_entitlement="videos.premium")
        premium_plan = self.make_plan("premium", entitlements=["videos.standard", "videos.premium"])
        self.active_membership(plan=premium_plan)
        self.client.force_login(self.member)
        self.assertContains(self.client.get(LIBRARY), "Premium clip")

    def test_dashboard_shows_latest_videos_to_members_only(self):
        self.client.force_login(self.member)
        locked = self.client.get(reverse("dashboard"))
        self.assertNotContains(locked, "Protected clip")
        self.assertNotContains(locked, CDN)
        self.assertContains(locked, "Videos are available to members with an active membership.")
        self.assertContains(locked, PLANS)
        self.active_membership()
        unlocked = self.client.get(reverse("dashboard"))
        self.assertContains(unlocked, "Protected clip")
        self.assertNotContains(unlocked, "Videos are available to members with an active membership.")

    def test_memberships_switched_off_hides_the_catalogue(self):
        self.active_membership()
        self.client.force_login(self.member)
        enable_memberships(False)
        self.assertEqual(self.client.get(LIBRARY).status_code, 404)
        self.assertEqual(self.client.get(self.detail).status_code, 404)

    def test_staff_video_management_is_unaffected(self):
        """Content managers have no membership: the member catalogue refuses them, while
        the console's own video pages keep working."""
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(LIBRARY).status_code, 302)
        console = self.client.get(reverse("administration:video_detail", args=[self.video.pk]))
        self.assertContains(console, "Protected clip")
        self.assertEqual(self.client.get(reverse("administration:videos")).status_code, 200)

    def test_expiry_by_time_without_the_background_job(self):
        """Access ends at expires_at even if the expiry job has not run yet."""
        subscription = self.active_membership()
        self.client.force_login(self.member)
        # Only the access decision's clock moves past expires_at (sessions and the database
        # guards keep real time); the row is still ACTIVE because no expiry job ran.
        later = subscription.expires_at + timedelta(seconds=1)
        with mock.patch("apps.memberships.access.timezone", SimpleNamespace(now=lambda: later)):
            self.assert_blocked("Videos are available to members with an active membership")
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Subscription.Status.ACTIVE)
