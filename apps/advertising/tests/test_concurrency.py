from datetime import timedelta
from unittest import skipUnless

from django.core.cache import cache
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from apps.accounts.tests.helpers import make_user
from apps.advertising import services
from apps.advertising.models import Campaign, Organization
from apps.audit.models import AuditEvent
from apps.core.models import SiteConfiguration
from apps.memberships.tests.test_concurrency import run_threads
from apps.videos import services as videos
from apps.videos.models import Video
from apps.videos.tests.helpers import BUNNY, User, guid


@BUNNY
@skipUnless(connection.vendor == "postgresql", "Concurrency is verified on PostgreSQL.")
class CampaignConcurrencyTests(TransactionTestCase):
    def setUp(self):
        SiteConfiguration.objects.update_or_create(id=1, defaults={"advertisers_enabled": True})
        cache.clear()
        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.owner = make_user("owner@brand.example")
        org = services.create_organization(self.owner, display_name="Brand", legal_name="Brand Ltd",
                                           contact_email="ads@brand.example", country="TZ")
        self.org = services.review_organization(self.root, org, "approve", expected_status="PENDING")
        video = videos.create_video(self.root, title="Clip")
        Video.objects.filter(pk=video.pk).update(provider_video_id=guid(), processing_status="READY")
        self.video = videos.publish(self.root, Video.objects.get(pk=video.pk))
        services.grant_video(self.root, self.org, self.video)

    def campaign(self, starts_in=timedelta(hours=-1)):
        now = timezone.now()
        campaign = services.create_campaign(self.owner, self.org, name="Race", video=self.video,
                                            starts_at=now + starts_in, ends_at=now + timedelta(days=2),
                                            max_views_per_member=1, demo_budget_amount=1000, demo_reward_per_view=10)
        return services.submit_campaign(self.owner, campaign)

    def test_concurrent_approvals_approve_once(self):
        campaign = self.campaign()
        results = run_threads(4, lambda i: services.approve_campaign(self.root, campaign, expected_revision=1))
        self.assertTrue(all(isinstance(r, Campaign) for r in results), results)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "ACTIVE")
        for action in ("campaign.approved", "campaign.activated"):
            self.assertEqual(AuditEvent.objects.filter(action=action, target_id=str(campaign.pk)).count(), 1)

    def test_conflicting_decisions_one_wins(self):
        campaign = self.campaign()

        def decide(i):
            if i % 2:
                return services.approve_campaign(self.root, campaign, expected_revision=1)
            return services.reject_campaign(self.root, campaign, expected_revision=1, reason="Not suitable")

        results = run_threads(4, decide)
        campaign.refresh_from_db()
        winners = AuditEvent.objects.filter(target_id=str(campaign.pk),
                                            action__in=["campaign.approved", "campaign.rejected"]).count()
        self.assertEqual(winners, 1, results)
        self.assertIn(campaign.status, ("ACTIVE", "REJECTED"))
        losers = [r for r in results if isinstance(r, services.InvalidState)]
        self.assertTrue(all(isinstance(r, (Campaign, services.InvalidState)) for r in results), results)
        self.assertGreaterEqual(len(losers), 1)

    def test_concurrent_schedule_runs_activate_once(self):
        campaign = self.campaign(starts_in=timedelta(hours=1))
        services.approve_campaign(self.root, campaign, expected_revision=1)
        later = timezone.now() + timedelta(hours=2)
        results = run_threads(3, lambda i: services.run_schedule(now=later))
        self.assertEqual(sum(r["activated"] for r in results), 1, results)
        self.assertEqual(Campaign.objects.get(pk=campaign.pk).status, "ACTIVE")

    def test_concurrent_applications_create_one_pending_org(self):
        applicant = make_user("new@shop.example")
        results = run_threads(3, lambda i: services.create_organization(
            applicant, display_name=f"Shop {i}", legal_name="Shop Ltd", contact_email="a@shop.example", country="TZ"))
        created = [r for r in results if isinstance(r, Organization)]
        self.assertEqual(len(created), 1, results)
        self.assertEqual(Organization.objects.filter(created_by=applicant).count(), 1)
