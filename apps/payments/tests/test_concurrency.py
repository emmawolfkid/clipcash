from datetime import timedelta
from unittest import skipUnless

from django.core.cache import cache
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from apps.accounts.tests.helpers import make_user
from apps.advertising import services as advertising
from apps.core.models import SiteConfiguration
from apps.memberships.tests.test_concurrency import run_threads
from apps.payments import services
from apps.payments.models import PaymentIntent, SandboxCampaignAllocation, SandboxPosting, SandboxProviderTransaction
from apps.payments.providers import SandboxProvider
from apps.videos import services as videos
from apps.videos.models import Video
from apps.videos.tests.helpers import BUNNY, User, guid

from .helpers import SANDBOX, signed_body, token


@BUNNY
@SANDBOX
@skipUnless(connection.vendor == "postgresql", "Concurrency is verified on PostgreSQL.")
class PaymentConcurrencyTests(TransactionTestCase):
    def setUp(self):
        SiteConfiguration.objects.update_or_create(id=1, defaults={"advertisers_enabled": True,
                                                                   "videos_enabled": True})
        cache.clear()
        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.owner = make_user("owner@brand.example")
        org = advertising.create_organization(self.owner, display_name="Brand", legal_name="Brand Ltd",
                                              contact_email="a@brand.example", country="TZ")
        self.org = advertising.review_organization(self.root, org, "approve", expected_status="PENDING")

    def paid_deposit(self, amount=50_000):
        intent = services.create_deposit_intent(self.owner, self.org, amount=amount, idempotency_key=token())
        SandboxProviderTransaction.objects.filter(provider_reference=intent.provider_reference).update(
            status="PAID", settled=True, sequence=1)
        return intent

    def campaign(self, name):
        video = videos.create_video(self.root, title=name)
        Video.objects.filter(pk=video.pk).update(provider_video_id=guid(), processing_status="READY",
                                                 duration_seconds=95)
        video = videos.publish(self.root, Video.objects.get(pk=video.pk))
        advertising.grant_video(self.root, self.org, video)
        now = timezone.now()
        campaign = advertising.create_campaign(self.owner, self.org, name=name, video=video,
                                               starts_at=now - timedelta(hours=1), ends_at=now + timedelta(days=2),
                                               max_views_per_member=1, demo_budget_amount=1000,
                                               demo_reward_per_view=10)
        campaign = advertising.submit_campaign(self.owner, campaign)
        return advertising.approve_campaign(self.root, campaign, expected_revision=1)

    def test_parallel_identical_callbacks_post_once(self):
        intent = self.paid_deposit()
        body, signature = signed_body(intent.provider_reference, 1)
        results = run_threads(5, lambda i: services.receive_callback("sandbox", body, signature))
        self.assertEqual(sum(1 for status, outcome in results if outcome == "DUPLICATE"), 4, results)
        intent.refresh_from_db()
        self.assertEqual(intent.status, "SETTLED")
        self.assertEqual(SandboxPosting.objects.filter(intent=intent).count(), 3)
        self.assertEqual(services.funds_for(self.org)["received"], 50_000)

    def test_callback_and_poll_race_post_once(self):
        intent = self.paid_deposit()
        provider = SandboxProvider()

        def deliver(i):
            if i % 2:
                return services.process_provider_update(provider, f"poll:{i}", intent.provider_reference, kind="POLL")
            body, signature = signed_body(intent.provider_reference, i)
            return services.receive_callback("sandbox", body, signature)

        run_threads(4, deliver)
        self.assertEqual(PaymentIntent.objects.get(pk=intent.pk).status, "SETTLED")
        self.assertEqual(SandboxPosting.objects.filter(intent=intent).count(), 3)
        self.assertEqual(services.funds_for(self.org)["received"], 50_000)

    def test_concurrent_allocations_never_overspend(self):
        intent = self.paid_deposit(30_000)
        services.process_provider_update(SandboxProvider(), "poll:1", intent.provider_reference, kind="POLL")
        campaigns = [self.campaign(f"C{i}") for i in range(4)]
        results = run_threads(4, lambda i: services.allocate(self.owner, self.org, campaigns[i], amount=10_000))
        self.assertEqual(sum(1 for r in results if isinstance(r, SandboxCampaignAllocation)), 3, results)
        self.assertEqual(services.funds_for(self.org)["available"], 0)
