"""FIN-01 concurrency on the real database (MariaDB here; PostgreSQL in CI). Real threads, real row
locks: capacity is never over-promised and nothing is posted twice."""

from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from apps.accounts.tests.helpers import make_user
from apps.advertising import services as advertising
from apps.core.models import SiteConfiguration
from apps.earnings import funding, services
from apps.earnings.models import CampaignDeposit, SessionReward
from apps.memberships import services as memberships
from apps.memberships.tests.helpers import payment_for, payments_enabled
from apps.memberships.tests.test_concurrency import run_threads
from apps.rewards import services as rewards
from apps.videos import services as videos
from apps.videos.models import Video
from apps.videos.tests.helpers import BUNNY, User, guid
from apps.viewing import services as viewing
from apps.viewing.tests.helpers import Clock
from apps.wallets.models import JournalEntry

from .helpers import ACTIVE, reference, staff


@BUNNY
@ACTIVE
@override_settings(VIEWING_MAX_EVENTS_PER_MINUTE=1000, VIEWING_MAX_STARTS_PER_HOUR=1000)
class EarningsConcurrencyTests(TransactionTestCase):
    def setUp(self):
        import importlib

        from django.apps import apps

        # TransactionTestCase flushes tables: restore the platform ledger accounts (wallets 0003).
        importlib.import_module("apps.wallets.migrations.0003_platform_accounts").create_platform_accounts(apps, None)
        SiteConfiguration.objects.update_or_create(id=1, defaults={
            "advertisers_enabled": True, "videos_enabled": True, "memberships_enabled": True})
        cache.clear()
        self.clock = Clock()
        patcher = mock.patch("django.utils.timezone.now", side_effect=lambda: self.clock.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.plan = memberships.create_plan(self.root, code="monthly", name="Monthly", price_amount=5000,
                                            duration_days=30, entitlements=["videos.standard"])
        owner = make_user("owner@brand.example")
        org = advertising.create_organization(owner, display_name="Brand", legal_name="Brand Ltd",
                                              contact_email="a@brand.example", country="TZ")
        self.org = advertising.review_organization(self.root, org, "approve", expected_status="PENDING")
        video = videos.create_video(self.root, title="Clip")
        Video.objects.filter(pk=video.pk).update(provider_video_id=guid(), processing_status="READY",
                                                 duration_seconds=95)
        video = videos.publish(self.root, Video.objects.get(pk=video.pk))
        advertising.grant_video(self.root, self.org, video)
        now = timezone.now()
        campaign = advertising.create_campaign(owner, self.org, name="Race", video=video,
                                               starts_at=now - timedelta(hours=1), ends_at=now + timedelta(days=2),
                                               max_views_per_member=1, demo_budget_amount=10_000,
                                               demo_reward_per_view=100)
        campaign = advertising.submit_campaign(owner, campaign)
        self.campaign = advertising.approve_campaign(self.root, campaign, expected_revision=1)
        self.recorder = staff("recorder@x.example", "earnings.record_deposits", "earnings.allocate_funding")
        self.verifier = staff("verifier@x.example", "earnings.verify_deposits")
        self.second_verifier = staff("verifier2@x.example", "earnings.verify_deposits")
        approver = staff("approver@x.example", "rewards.approve_reward_policies")
        rewards.approve_policy(approver, rewards.create_policy(self.root, self.campaign, reward_amount=100, kind="TZS"))

    def member(self, index):
        member = make_user(f"m{index}@x.example")
        with payments_enabled():
            pending = memberships.choose_plan(member, self.plan)
            memberships.activate_with_verified_payment(pending.pk, payment_for(pending))
        return member

    def deposit(self, amount, verify=True):
        deposit = funding.record_deposit(self.recorder, organization=self.org, method="AIRTEL_LIPA", amount=amount,
                                         external_reference=reference(), paid_on=timezone.localdate())
        return funding.verify_deposit(self.verifier, deposit, receipt_confirmed=True) if verify else deposit

    def verified_reward(self):
        member = self.member(99)
        session = viewing.start_session(member, self.campaign.pk)
        viewing.record_event(member, session.pk, {"seq": 1, "position_ms": 0, "state": "playing"})
        for seq in range(2, 12):
            self.clock.advance(10)
            viewing.record_event(member, session.pk, {"seq": seq, "position_ms": min(95, (seq - 1) * 10) * 1000,
                                                      "state": "playing"})
        with mock.patch("apps.earnings.services.settle_safely"):  # Leave it RESERVED for the race.
            viewing.record_event(member, session.pk, {"seq": 12, "position_ms": 95_000, "state": "ended"})
        reward = SessionReward.objects.get(session=session)
        assert reward.status == "RESERVED"
        return reward

    def test_racing_members_never_over_promise_funding(self):
        self.deposit(200)
        funding.commit_funding(self.recorder, self.campaign, amount=200)  # Exactly two TZS 100 views.
        members = [self.member(i) for i in range(6)]
        results = run_threads(6, lambda i: viewing.start_session(members[i], self.campaign.pk))
        started = [r for r in results if not isinstance(r, Exception)]
        refused = [r for r in results if isinstance(r, viewing.Denied)]
        self.assertEqual(len(started), 2, results)
        self.assertEqual(len(refused), 4, results)
        self.assertTrue(all(r.code == "reward_funding_unavailable" for r in refused))
        self.assertEqual(SessionReward.objects.filter(status="RESERVED").count(), 2)
        self.assertEqual(funding.campaign_summary(self.campaign)["remaining"], 0)

    def test_parallel_settlement_and_clearance_post_once(self):
        self.deposit(1_000)
        funding.commit_funding(self.recorder, self.campaign, amount=1_000)
        reward = self.verified_reward()
        run_threads(5, lambda i: services.settle(reward.pk))
        self.assertEqual(JournalEntry.objects.filter(posting_type="reward.pending").count(), 1)
        self.clock.advance(25 * 3600)
        run_threads(5, lambda i: services.clear_due())
        self.assertEqual(JournalEntry.objects.filter(posting_type="reward.cleared").count(), 1)
        reward.refresh_from_db()
        self.assertEqual(reward.status, "AVAILABLE")
        self.assertEqual(services.check_consistency(), [])

    def test_two_verifiers_post_a_deposit_once(self):
        deposit = self.deposit(5_000, verify=False)
        verifiers = [self.verifier, self.second_verifier]
        run_threads(2, lambda i: funding.verify_deposit(verifiers[i], deposit, receipt_confirmed=True))
        self.assertEqual(JournalEntry.objects.filter(posting_type="advertiser.manual_deposit_verified").count(), 1)
        self.assertEqual(CampaignDeposit.objects.get().status, "VERIFIED")
        self.assertEqual(funding.organization_balance(self.org), 5_000)

    def test_parallel_commitments_cannot_overspend_the_organization(self):
        self.deposit(1_000)
        results = run_threads(3, lambda i: funding.commit_funding(self.recorder, self.campaign, amount=600))
        self.assertEqual(sum(1 for r in results if isinstance(r, JournalEntry)), 1, results)
        self.assertEqual(funding.organization_balance(self.org), 400)
        self.assertEqual(funding.campaign_summary(self.campaign)["committed"], 600)
