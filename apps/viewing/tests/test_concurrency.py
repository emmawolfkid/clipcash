from datetime import timedelta
from unittest import skipUnless

from django.core.cache import cache
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from apps.accounts.tests.helpers import make_user
from apps.advertising import services as advertising
from apps.audit.models import AuditEvent
from apps.core.models import SiteConfiguration
from apps.memberships import services as memberships
from apps.memberships.tests.helpers import payment_for, payments_enabled
from apps.memberships.tests.test_concurrency import run_threads
from apps.videos import services as videos
from apps.videos.models import Video
from apps.videos.tests.helpers import BUNNY, User, guid
from apps.viewing import services
from apps.viewing.models import WatchSession


@BUNNY
@override_settings(VIEWING_MAX_EVENTS_PER_MINUTE=120)
@skipUnless(connection.vendor == "postgresql", "Concurrency is verified on PostgreSQL.")
class ViewingConcurrencyTests(TransactionTestCase):
    def setUp(self):
        SiteConfiguration.objects.update_or_create(id=1, defaults={
            "advertisers_enabled": True, "videos_enabled": True, "memberships_enabled": True})
        cache.clear()
        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.member = make_user("member@example.com")
        plan = memberships.create_plan(self.root, code="monthly", name="Monthly", price_amount=5000,
                                       duration_days=30, entitlements=["videos.standard"])
        with payments_enabled():
            pending = memberships.choose_plan(self.member, plan)
            memberships.activate_with_verified_payment(pending.pk, payment_for(pending))
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
                                               max_views_per_member=1, demo_budget_amount=1000,
                                               demo_reward_per_view=10)
        campaign = advertising.submit_campaign(owner, campaign)
        self.campaign = advertising.approve_campaign(self.root, campaign, expected_revision=1)

    def test_parallel_starts_create_one_session(self):
        results = run_threads(5, lambda i: services.start_session(self.member, self.campaign.pk))
        self.assertTrue(all(isinstance(r, WatchSession) for r in results), results)
        self.assertEqual(len({r.pk for r in results}), 1)
        self.assertEqual(WatchSession.objects.count(), 1)

    def test_parallel_finish_finalizes_once(self):
        session = services.start_session(self.member, self.campaign.pk)
        services.record_event(self.member, session.pk, {"seq": 1, "position_ms": 0, "state": "playing"})
        results = run_threads(4, lambda i: services.finish_session(self.member, session.pk))
        self.assertTrue(all(isinstance(r, WatchSession) for r in results), results)
        self.assertEqual(AuditEvent.objects.filter(action="viewing.session.finalized").count(), 1)

    def test_parallel_identical_events_counted_once(self):
        session = services.start_session(self.member, self.campaign.pk)
        results = run_threads(4, lambda i: services.record_event(
            self.member, session.pk, {"seq": 1, "position_ms": 0, "state": "playing"}))
        session.refresh_from_db()
        self.assertEqual(session.event_count, 1, results)
        self.assertEqual(sum(1 for r in results if r.code == "ok"), 1)

    def test_parallel_expiry_jobs_close_once(self):
        session = services.start_session(self.member, self.campaign.pk)
        later = timezone.now() + timedelta(hours=1)
        results = run_threads(3, lambda i: services.expire_sessions(now=later))
        self.assertEqual(sum(r["expired"] for r in results), 1, results)
        session.refresh_from_db()
        self.assertEqual(session.state, "EXPIRED")
