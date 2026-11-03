"""Phase 14-C: the complete demonstration journey through the real services and pages,
and the read-only demo readiness check."""

import json
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.administration.management.commands.demo_readiness import FAIL, readiness
from apps.advertising import services as advertising
from apps.memberships import services as memberships
from apps.memberships.tests.test_complimentary import GRANTS, financial_snapshot
from apps.rewards import services as rewards
from apps.rewards.models import DemoReward
from apps.rewards.tests.helpers import RewardsTestCase, make_user
from apps.viewing.models import WatchSession
from apps.wallets import wallet_services

SAMPLE = "/static/videos/sample.mp4"
DEMO_ENVIRONMENT = {**GRANTS, "VIDEO_PROVIDER": "demo", "VIDEO_DEMO_ALLOWED": True, "VIDEO_DEMO_SAMPLE_URL": SAMPLE}


@override_settings(**DEMO_ENVIRONMENT)
class DemoJourneyTests(RewardsTestCase):
    def setUp(self):
        super().setUp()
        self.policy = self.approved_policy()  # Drafted by one Super Administrator, approved by another.
        self.visitor = make_user("journey@example.com")

    def heartbeat(self, session, seq, position, state="playing", after=10):
        self.clock.advance(after)
        return self.client.post(reverse("viewing:events", args=[session.pk]),
                                json.dumps({"seq": seq, "position_ms": position * 1000, "state": state}),
                                content_type="application/json")

    def test_member_journey_from_grant_to_simulated_reward_without_money(self):
        wallet = wallet_services.ensure_wallet(self.visitor)  # Opens three ledger accounts at zero.
        before = financial_snapshot()
        expires = self.clock.now + timedelta(days=2)
        granted = memberships.grant_complimentary(self.root, self.visitor, self.plan, expires_at=expires,
                                                  reason="Client demonstration", operation_id="journey-0001")
        self.assertEqual((granted.activation_source, granted.amount_paid), ("COMPLIMENTARY", 0))

        self.client.force_login(self.visitor)
        self.assertContains(self.client.get(reverse("viewing:list")), self.campaign.name)
        started = self.client.post(reverse("viewing:campaign", args=[self.campaign.pk]))
        session = WatchSession.objects.get(member=self.visitor)
        self.assertRedirects(started, reverse("viewing:session", args=[session.pk]))
        page = self.client.get(reverse("viewing:session", args=[session.pk]))
        self.assertContains(page, 'id="watch-video"')
        self.assertContains(page, SAMPLE)

        seq = 1
        self.assertEqual(self.heartbeat(session, seq, 0, after=1).status_code, 200)
        for position in range(10, 100, 10):
            seq += 1
            self.assertEqual(self.heartbeat(session, seq, min(position, 95)).status_code, 200)
        self.heartbeat(session, seq + 1, 95, state="ended", after=1)
        session.refresh_from_db()
        self.assertEqual(session.outcome, WatchSession.Outcome.VERIFIED_EVIDENCE, session.reasons)

        rewards.evaluate_pending()
        reward = DemoReward.objects.get(member=self.visitor)
        self.assertEqual((reward.status, reward.amount), (DemoReward.Status.QUALIFIED, self.policy.reward_amount))
        self.assertContains(self.client.get(reverse("rewards:history")), self.campaign.name)

        report = advertising.campaign_report(self.campaign)
        self.assertEqual((report["viewing"]["verified"]["value"], report["viewing"]["viewers"]["value"]), (1, 1))

        balances = wallet_services.balances(wallet)
        self.assertEqual((balances.available, balances.pending, balances.reserved), (0, 0, 0))
        after = financial_snapshot()
        self.assertTrue(all(balance == 0 for _, balance in after["ledger_balances"]))
        changed = {key for key in before if before[key] != after[key]}
        # Only the simulated reward records and their demo budget counter may appear.
        self.assertEqual(changed, {"DemoReward", "CampaignRewardBudget"})
        self.assertEqual((after["JournalEntry"], after["PaymentIntent"], after["WithdrawalRequest"],
                          after["funded_campaigns"]), (before["JournalEntry"], before["PaymentIntent"],
                                                       before["WithdrawalRequest"], 0))

    def test_access_ends_when_the_complimentary_grant_expires(self):
        expires = self.clock.now + timedelta(hours=1)
        memberships.grant_complimentary(self.root, self.visitor, self.plan, expires_at=expires,
                                        reason="Short demonstration")
        self.assertTrue(advertising.campaign_eligibility(self.campaign, self.visitor))
        self.clock.now = expires + timedelta(seconds=1)
        decision = advertising.campaign_eligibility(self.campaign, self.visitor)
        self.assertEqual((bool(decision), decision.reason), (False, "no_membership"))
        self.client.force_login(self.visitor)
        self.client.post(reverse("viewing:campaign", args=[self.campaign.pk]))
        self.assertFalse(WatchSession.objects.filter(member=self.visitor).exists())


@override_settings(**DEMO_ENVIRONMENT)
class DemoReadinessTests(RewardsTestCase):
    def failures(self):
        return [(area, detail) for status, area, detail in readiness() if status == FAIL]

    def test_prepared_environment_has_no_failures(self):
        self.approved_policy()
        self.assertEqual(self.failures(), [])

    @override_settings(VIDEO_DEMO_SAMPLE_URL="")
    def test_missing_sample_clip_fails(self):
        self.assertIn("Demo sample clip", [area for area, _ in self.failures()])

    @override_settings(MEMBERSHIPS_COMPLIMENTARY_GRANTS_ENABLED=False)
    def test_no_way_to_give_access_fails_but_existing_access_only_warns(self):
        self.assertNotIn("Complimentary demo grants", [area for area, _ in self.failures()])  # A member has access.
        from apps.memberships.models import Subscription

        memberships.cancel(self.root, Subscription.objects.get(member=self.member, status="ACTIVE"), "ADMIN_REVIEW")
        self.assertIn("Complimentary demo grants", [area for area, _ in self.failures()])

    def test_ineligible_campaign_fails(self):
        advertising.pause_campaign(self.root, self.campaign, reason="Staff check")
        self.assertIn("Sponsored campaigns", [area for area, _ in self.failures()])

    def test_command_is_read_only_and_exits_non_zero_on_failure(self):
        with CaptureQueriesContext(connection) as queries, override_settings(VIDEO_DEMO_SAMPLE_URL=""):
            with self.assertRaises(SystemExit) as ctx:
                call_command("demo_readiness", stdout=StringIO())
        self.assertEqual(ctx.exception.code, 1)
        writes = [q["sql"] for q in queries.captured_queries
                  if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(writes, [])
        self.approved_policy()
        out = StringIO()
        call_command("demo_readiness", stdout=out)
        self.assertIn("READY", out.getvalue())
        self.assertIn("Financial locks", out.getvalue())
