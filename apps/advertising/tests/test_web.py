import uuid

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.advertising import services
from apps.advertising.models import Campaign, Organization
from apps.videos.tests.helpers import API_KEY, TOKEN_KEY

from .helpers import AdvertisingTestCase, make_user

DT = "%Y-%m-%dT%H:%M"


class TenantIsolationTests(AdvertisingTestCase):
    def setUp(self):
        super().setUp()
        self.campaign = self.make_campaign()
        self.rival = make_user("rival@other.example")
        self.rival_org = self.make_org(self.rival, "Rival")

    def urls(self, org, campaign):
        return [reverse("advertising:organization", args=[org]), reverse("advertising:profile", args=[org]),
                reverse("advertising:team", args=[org]), reverse("advertising:campaigns", args=[org]),
                reverse("advertising:campaign_create", args=[org]),
                reverse("advertising:campaign", args=[org, campaign]),
                reverse("advertising:campaign_edit", args=[org, campaign])]

    def test_other_advertisers_get_not_found(self):
        self.client.force_login(self.rival)
        for url in self.urls(self.org.pk, self.campaign.pk) + self.urls(uuid.uuid4(), uuid.uuid4()):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
        # Own organization, but another organization's campaign id: also not found.
        url = reverse("advertising:campaign", args=[self.rival_org.pk, self.campaign.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        action = reverse("advertising:campaign_action", args=[self.org.pk, self.campaign.pk, "submit"])
        self.assertEqual(self.client.post(action).status_code, 404)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, "DRAFT")

    def test_owner_pages_render(self):
        self.client.force_login(self.owner)
        for url in self.urls(self.org.pk, self.campaign.pk):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_get_and_csrf_do_not_mutate(self):
        action = reverse("advertising:campaign_action", args=[self.org.pk, self.campaign.pk, "submit"])
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(action).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        self.assertEqual(csrf_client.post(action).status_code, 403)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, "DRAFT")

    def test_unverified_and_anonymous(self):
        url = reverse("advertising:home")
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(make_user("u@x.example", verified=False))
        self.assertRedirects(self.client.get(url), reverse("accounts:verification_required"))


class AdvertiserFlowTests(AdvertisingTestCase):
    def test_onboard_create_submit_via_web(self):
        applicant = make_user("web@shop.example")
        self.client.force_login(applicant)
        response = self.client.post(reverse("advertising:onboard"), {
            "display_name": "Web Shop", "legal_name": "Web Shop Ltd", "contact_email": "hi@shop.example",
            "country": "tz", "contact_phone": "", "website": "", "registration_number": "", "tax_id": ""})
        org = Organization.objects.get(display_name="Web Shop")
        self.assertRedirects(response, reverse("advertising:organization", args=[org.pk]))
        self.assertEqual(org.country, "TZ")
        services.review_organization(self.root, org, "approve", expected_status="PENDING")
        services.grant_video(self.root, org, self.ad_video)
        now = timezone.localtime()
        response = self.client.post(reverse("advertising:campaign_create", args=[org.pk]), {
            "name": "Web campaign", "description": "", "video": self.ad_video.pk,
            "starts_at": now.strftime(DT), "ends_at": (now + timezone.timedelta(days=3)).strftime(DT),
            "target_plans": [self.plan.pk], "max_views_per_member": 1, "demo_budget_amount": "50000",
            "demo_reward_per_view": "50"})
        campaign = Campaign.objects.get(name="Web campaign")
        self.assertRedirects(response, reverse("advertising:campaign", args=[org.pk, campaign.pk]))
        page = self.client.get(reverse("advertising:campaign", args=[org.pk, campaign.pk]))
        self.assertContains(page, "Demonstration budgets only")
        self.assertContains(page, "Not funded")
        self.client.post(reverse("advertising:campaign_action", args=[org.pk, campaign.pk, "submit"]))
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "SUBMITTED")
        # Fractional budgets are refused by the form.
        bad = self.client.post(reverse("advertising:campaign_create", args=[org.pk]), {
            "name": "Bad", "video": self.ad_video.pk, "starts_at": now.strftime(DT),
            "ends_at": (now + timezone.timedelta(days=3)).strftime(DT), "max_views_per_member": 1,
            "demo_budget_amount": "100.50", "demo_reward_per_view": "1"})
        self.assertEqual(bad.status_code, 200)
        self.assertFalse(Campaign.objects.filter(name="Bad").exists())

    def test_stale_edit_and_cancel_confirmation(self):
        campaign = self.make_campaign()
        self.client.force_login(self.owner)
        edit = reverse("advertising:campaign_edit", args=[self.org.pk, campaign.pk])
        form = {"name": "Renamed", "description": "", "video": self.ad_video.pk,
                "starts_at": timezone.localtime(campaign.starts_at).strftime(DT),
                "ends_at": timezone.localtime(campaign.ends_at).strftime(DT), "max_views_per_member": 1,
                "demo_budget_amount": "100000", "demo_reward_per_view": "100", "revision": "1"}
        self.client.post(edit, form)
        campaign.refresh_from_db()
        self.assertEqual((campaign.name, campaign.revision), ("Renamed", 2))
        response = self.client.post(edit, {**form, "name": "Stale", "revision": "1"})
        self.assertContains(response, "changed since you opened it")
        cancel = reverse("advertising:campaign_action", args=[self.org.pk, campaign.pk, "cancel"])
        self.client.post(cancel)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "DRAFT")
        self.client.post(cancel, {"confirm": "yes"})
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "CANCELLED")

    def test_reports_are_tenant_scoped_and_honest(self):
        self.running_campaign()
        rival = make_user("r@x.example")
        rival_org = self.make_org(rival, "Rival")
        services.grant_video(self.root, rival_org, self.ad_video)
        self.make_campaign(user=rival, org=rival_org, name="Rival draft")
        self.client.force_login(self.owner)
        page = self.client.get(reverse("advertising:organization", args=[self.org.pk]))
        report = page.context["report"]
        self.assertEqual((report["total"], report["n_active"], report["n_draft"]), (1, 1, 0))
        self.assertEqual(report["demo_budget_running"], 100_000)
        # Phase 14-C: real verified-view figures (none yet), and no invented spend.
        self.assertEqual(report["viewing"]["verified"]["value"], 0)
        self.assertNotContains(page, "not available yet")
        self.assertContains(page, "Spend: not applicable")
        self.assertNotContains(page, "Rival draft")

    def test_list_search_filter_and_pagination(self):
        for index in range(22):
            self.make_campaign(name=f"Promo {index}")
        self.client.force_login(self.owner)
        url = reverse("advertising:campaigns", args=[self.org.pk])
        response = self.client.get(url, {"status": "DRAFT", "q": "Promo"})
        self.assertEqual(len(response.context["page"].object_list), 20)
        self.assertContains(response, "status=DRAFT&amp;q=Promo&amp;page=2")
        self.assertEqual(len(self.client.get(url, {"q": "Promo 21"}).context["page"].object_list), 1)

    def test_campaign_list_has_no_n_plus_one(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.make_campaign(name="First")
        self.client.force_login(self.owner)
        url = reverse("advertising:campaigns", args=[self.org.pk])

        def count():
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(url).status_code, 200)
            return len(queries)

        count()
        baseline = count()
        for index in range(6):
            self.make_campaign(name=f"More {index}")
        self.assertEqual(count(), baseline)

    def test_preview_uses_signed_player_without_credentials(self):
        campaign = self.make_campaign()
        self.client.force_login(self.owner)
        page = self.client.get(reverse("advertising:campaign", args=[self.org.pk, campaign.pk]))
        self.assertContains(page, f"https://player.mediadelivery.net/embed/12345/{self.ad_video.provider_video_id}")
        self.assertContains(page, "token=")
        self.assertNotContains(page, API_KEY)
        self.assertNotContains(page, TOKEN_KEY)


class ConsoleTests(AdvertisingTestCase):
    def test_access_by_role(self):
        self.client.force_login(self.manager)  # Content Manager: no advertising permissions.
        for name in ("advertisers", "campaigns"):
            self.assertEqual(self.client.get(reverse(f"administration:{name}")).status_code, 403)
        self.assertEqual(self.client.get(reverse("administration:advertiser_review", args=[self.org.pk])).status_code,
                         403)
        self.client.force_login(self.owner)  # Not staff.
        self.assertEqual(self.client.get(reverse("administration:advertisers")).status_code, 302)
        self.client.force_login(self.root)
        for name in ("advertisers", "campaigns", "overview"):
            self.assertEqual(self.client.get(reverse(f"administration:{name}")).status_code, 200)
        self.assertEqual(self.client.get(reverse("administration:advertiser_review", args=[self.org.pk])).status_code,
                         200)

    def test_review_queue_and_decisions(self):
        pending = self.make_org(make_user("q@x.example"), "Queued")
        self.client.force_login(self.root)
        queue = self.client.get(reverse("administration:advertisers"))
        self.assertContains(queue, "Queued")
        self.assertNotContains(queue, ">Brand<")  # Approved ones are not in the default queue.
        url = reverse("administration:advertiser_review", args=[pending.pk])
        self.client.post(url, {"action": "approve", "expected_status": "PENDING"})
        pending.refresh_from_db()
        self.assertEqual(pending.verification_status, "APPROVED")
        response = self.client.post(url, {"action": "suspend", "expected_status": "PENDING", "reason": "Old view"},
                                    follow=True)
        self.assertContains(response, "changed since you opened it")
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_campaign_approval_via_console(self):
        campaign = services.submit_campaign(self.owner, self.make_campaign())
        self.client.force_login(self.root)
        self.assertContains(self.client.get(reverse("administration:campaigns")), campaign.name)
        url = reverse("administration:campaign_review", args=[campaign.pk])
        detail = self.client.get(url)
        self.assertContains(detail, "token=")
        self.client.post(url, {"action": "approve", "expected_revision": "1"})
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "ACTIVE")
        self.client.post(url, {"action": "cancel", "expected_revision": "1", "reason": "Policy issue"})
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "ACTIVE")  # No confirmation tick.
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_overview_counts(self):
        services.submit_campaign(self.owner, self.make_campaign())
        self.client.force_login(self.root)
        advertising = self.client.get(reverse("administration:overview")).context["sections"]["advertising"]
        self.assertEqual((advertising["organizations"]["APPROVED"], advertising["campaigns"]["SUBMITTED"]), (1, 1))
