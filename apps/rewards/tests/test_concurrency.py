from datetime import timedelta
from unittest import skipUnless

from django.core.cache import cache
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from apps.accounts.tests.helpers import make_user
from apps.advertising import services as advertising
from apps.audit.models import AuditEvent
from apps.core.models import SiteConfiguration
from apps.memberships import services as memberships
from apps.memberships.tests.helpers import payment_for, payments_enabled
from apps.memberships.tests.test_concurrency import run_threads
from apps.rewards import services
from apps.rewards.models import DemoReward
from apps.videos import services as videos
from apps.videos.models import Video
from apps.videos.tests.helpers import BUNNY, User, guid
from apps.viewing import services as viewing
from apps.viewing.models import WatchSession


def verified_session(member, campaign):
    """A finalized VERIFIED session written through the trigger-checked transitions."""
    now = timezone.now()
    session = WatchSession.objects.create(member=member, campaign=campaign, campaign_revision=campaign.revision,
                                          video=campaign.video, start_deadline=now + timedelta(minutes=5),
                                          expires_at=now + timedelta(hours=1), video_duration_ms=95_000,
                                          required_ms=85_500)
    WatchSession.objects.filter(pk=session.pk).update(state="IN_PROGRESS", started_at=now, credited_ms=90_000,
                                                      coverage_ms=90_000, event_count=10, last_sequence=10)
    WatchSession.objects.filter(pk=session.pk).update(state="FINALIZED", outcome="VERIFIED_EVIDENCE",
                                                      qualifying_slot=1, finalized_at=now + timedelta(seconds=1),
                                                      reasons=["thresholds_met"])
    return WatchSession.objects.get(pk=session.pk)


@BUNNY
@skipUnless(connection.vendor == "postgresql", "Concurrency is verified on PostgreSQL.")
class RewardConcurrencyTests(TransactionTestCase):
    def setUp(self):
        SiteConfiguration.objects.update_or_create(id=1, defaults={
            "advertisers_enabled": True, "videos_enabled": True, "memberships_enabled": True})
        cache.clear()
        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.approver = make_user("approver@example.com", role=User.Role.SUPER_ADMIN)
        self.plan = memberships.create_plan(self.root, code="monthly", name="Monthly", price_amount=5000,
                                            duration_days=30, entitlements=["videos.standard"])
        owner = make_user("owner@brand.example")
        org = advertising.create_organization(owner, display_name="Brand", legal_name="Brand Ltd",
                                              contact_email="a@brand.example", country="TZ")
        org = advertising.review_organization(self.root, org, "approve", expected_status="PENDING")
        video = videos.create_video(self.root, title="Clip")
        Video.objects.filter(pk=video.pk).update(provider_video_id=guid(), processing_status="READY",
                                                 duration_seconds=95)
        video = videos.publish(self.root, Video.objects.get(pk=video.pk))
        advertising.grant_video(self.root, org, video)
        now = timezone.now()
        campaign = advertising.create_campaign(owner, org, name="Race", video=video,
                                               starts_at=now - timedelta(hours=1), ends_at=now + timedelta(days=2),
                                               max_views_per_member=1, demo_budget_amount=300,
                                               demo_reward_per_view=100)
        campaign = advertising.submit_campaign(owner, campaign)
        self.campaign = advertising.approve_campaign(self.root, campaign, expected_revision=1)
        policy = services.create_policy(self.root, self.campaign, reward_amount=100)
        services.approve_policy(self.approver, policy)

    def member(self, email):
        member = make_user(email)
        with payments_enabled():
            pending = memberships.choose_plan(member, self.plan)
            memberships.activate_with_verified_payment(pending.pk, payment_for(pending))
        return member

    def test_parallel_evaluation_records_one_decision(self):
        session = verified_session(self.member("m@x.example"), self.campaign)
        results = run_threads(5, lambda i: services.evaluate_session(session.pk))
        self.assertTrue(all(isinstance(r, DemoReward) for r in results), results)
        self.assertEqual(DemoReward.objects.count(), 1)
        self.assertEqual(services.budget_summary(self.campaign)["demo_consumed"], 100)

    def test_concurrent_budget_exhaustion(self):
        sessions = [verified_session(self.member(f"m{i}@x.example"), self.campaign) for i in range(5)]
        run_threads(5, lambda i: services.evaluate_session(sessions[i].pk))
        self.assertEqual(DemoReward.objects.filter(status="DEMO_QUALIFIED").count(), 3)
        self.assertEqual(DemoReward.objects.filter(status="DEMO_REJECTED", reasons=["budget_exhausted"]).count(), 2)
        self.assertEqual(services.budget_summary(self.campaign),
                         {"demo_allocated": 300, "demo_consumed": 300, "demo_remaining": 0})

    def test_parallel_jobs_and_reversals_release_once(self):
        session = verified_session(self.member("m@x.example"), self.campaign)
        run_threads(3, lambda i: services.evaluate_pending())
        reward = DemoReward.objects.get()

        def reverse(i):
            if i % 2:
                return viewing.revoke_verified_session(self.root, session, reason="Fraud confirmed")
            return services.reverse_reward(self.approver, reward, reason="Duplicate account")

        results = run_threads(4, reverse)
        reward.refresh_from_db()
        self.assertEqual(reward.status, "DEMO_REVERSED", results)
        self.assertEqual(services.budget_summary(self.campaign)["demo_consumed"], 0)
        self.assertEqual(AuditEvent.objects.filter(action="rewards.reward.reversed").count(), 1)
