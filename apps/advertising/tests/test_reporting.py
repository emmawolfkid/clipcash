"""Phase 14-C (audit CC-010): verified-viewing results in advertiser and staff campaign reports."""

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.advertising import services
from apps.rewards import services as rewards
from apps.rewards.tests.helpers import RewardsTestCase
from apps.viewing import services as viewing
from apps.viewing.services import campaign_performance


@override_settings(ADVERTISING_REPORT_MIN_COUNT=0)  # Calculation tests; D12 masking is tested on its own.
class CampaignReportTests(RewardsTestCase):
    def figures(self, report):
        return {key: figure["value"] for key, figure in report["viewing"].items()}

    def test_counts_come_from_server_decided_outcomes(self):
        self.approved_policy()
        self.verified_session()
        second = self.new_member("second@example.com")
        self.verified_session(member=second)
        unfinished = self.new_member("third@example.com")
        self.start(member=unfinished)  # Started, not finished: a session, not a verified view.
        rewards.evaluate_pending()
        report = services.campaign_report(self.campaign)
        self.assertEqual(self.figures(report), {"sessions": 3, "verified": 2, "viewers": 2, "in_review": 0,
                                                "not_verified": 0})
        self.assertEqual(report["demo_rewards"]["qualified"]["value"], 2)
        self.assertEqual((report["demo_rewards"]["demo_consumed"], report["demo_rewards"]["demo_allocated"]),
                         (200, 100_000))
        org = services.organization_report(self.org)
        self.assertEqual((org["viewing"]["verified"]["value"], org["viewing"]["viewers"]["value"]), (2, 2))

    def test_revoked_views_are_not_counted(self):
        session = self.verified_session()
        viewing.revoke_verified_session(self.root, session, reason="Fraud confirmed")
        figures = self.figures(services.campaign_report(self.campaign))
        self.assertEqual((figures["verified"], figures["viewers"], figures["not_verified"]), (0, 0, 1))

    def test_reports_are_limited_to_the_organization(self):
        other_owner = self.new_member("other-owner@example.com")
        other_org = services.review_organization(self.root, self.make_org(other_owner, "Rival"), "approve",
                                                 expected_status="PENDING")
        from apps.advertising.services import grant_video

        grant_video(self.root, other_org, self.ad_video)
        rival = services.submit_campaign(other_owner, self.make_campaign(user=other_owner, org=other_org))
        rival = services.approve_campaign(self.root, rival, expected_revision=rival.revision)
        self.verified_session(campaign=rival)
        self.assertEqual(services.organization_report(self.org)["viewing"]["verified"]["value"], 0)
        self.assertEqual(services.organization_report(other_org)["viewing"]["verified"]["value"], 1)
        self.assertEqual(self.figures(services.campaign_report(self.campaign))["sessions"], 0)

    def test_performance_is_two_queries_whatever_the_campaign_count(self):
        for _ in range(3):
            self.running_campaign()
        with CaptureQueriesContext(connection) as queries:
            totals, by_campaign = campaign_performance(self.org.campaigns.all())
        self.assertEqual(len(queries), 2)
        self.assertEqual(totals["sessions"], 0)

    @override_settings(ADVERTISING_REPORT_MIN_COUNT=5)
    def test_small_counts_are_suppressed_for_advertisers_only(self):
        self.verified_session()
        advertiser = services.campaign_report(self.campaign)
        staff = services.campaign_report(self.campaign, for_advertiser=False)
        self.assertEqual(advertiser["viewing"]["verified"], {"value": None, "display": "fewer than 5"})
        self.assertEqual(staff["viewing"]["verified"], {"value": 1, "display": "1"})
        self.assertEqual(advertiser["viewing"]["in_review"]["display"], "0")  # Zero is not identifying.


@override_settings(ADVERTISING_REPORT_MIN_COUNT=0)  # Calculation tests; D12 masking is tested on its own.
class ReportPageTests(RewardsTestCase):
    def test_advertiser_pages_show_results_and_separate_demo_figures(self):
        self.approved_policy()
        self.verified_session()
        rewards.evaluate_pending()
        self.client.force_login(self.owner)
        overview = self.client.get(reverse("advertising:organization", args=[self.org.pk]))
        self.assertContains(overview, "Verified views")
        self.assertContains(overview, "Spend: not applicable")
        self.assertNotContains(overview, "not available yet")
        detail = self.client.get(reverse("advertising:campaign", args=[self.org.pk, self.campaign.pk]))
        report = detail.context["performance"]
        self.assertEqual((report["viewing"]["verified"]["value"], report["demo_rewards"]["qualified"]["value"]), (1, 1))
        self.assertContains(detail, "Demo rewards (simulated, not money)")
        self.assertContains(detail, "Demo budget used (simulated figure, never charged)")
        self.assertContains(detail, "TZS 100 of TZS 100,000")

    def test_staff_campaign_page_shows_exact_results(self):
        self.verified_session()
        self.client.force_login(self.root)
        with override_settings(ADVERTISING_REPORT_MIN_COUNT=5):
            response = self.client.get(reverse("administration:campaign_review", args=[self.campaign.pk]))
        self.assertEqual(response.context["performance"]["viewing"]["verified"]["display"], "1")
        self.assertContains(response, "Verified views")

    def test_other_advertisers_cannot_open_the_report(self):
        outsider = self.new_member("outsider@example.com")
        self.client.force_login(outsider)
        response = self.client.get(reverse("advertising:campaign", args=[self.org.pk, self.campaign.pk]))
        self.assertEqual(response.status_code, 404)
