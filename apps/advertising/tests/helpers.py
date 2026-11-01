from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from apps.advertising import services
from apps.advertising.models import Campaign
from apps.core.models import SiteConfiguration
from apps.videos.tests.helpers import BUNNY, PASSWORD, User, VideoTestCase, make_user


def enable_advertisers(enabled=True):
    SiteConfiguration.objects.filter(id=1).update(advertisers_enabled=enabled)
    cache.clear()


@BUNNY
class AdvertisingTestCase(VideoTestCase):
    """Root (Super Administrator) reviews; ``owner`` runs the approved advertiser ``org``."""

    def setUp(self):
        super().setUp()
        enable_advertisers()
        self.owner = make_user("owner@brand.example")
        self.org = self.make_org(self.owner, "Brand")
        self.org = services.review_organization(self.root, self.org, "approve", expected_status="PENDING")
        self.ad_video = self.published_video("Brand clip")
        services.grant_video(self.root, self.org, self.ad_video)

    def make_org(self, user, name, **values):
        defaults = {"display_name": name, "legal_name": f"{name} Limited", "contact_email": "ads@example.com",
                    "country": "TZ", "tax_id": "TIN-123-456-789"}
        return services.create_organization(user, **{**defaults, **values})

    def campaign_values(self, **values):
        now = timezone.now()
        defaults = {"name": "Launch", "description": "Sponsored clip", "video": self.ad_video,
                    "starts_at": now - timedelta(hours=1), "ends_at": now + timedelta(days=7),
                    "max_views_per_member": 1, "demo_budget_amount": 100_000, "demo_reward_per_view": 100}
        return {**defaults, **values}

    def make_campaign(self, user=None, org=None, **values):
        return services.create_campaign(user or self.owner, org or self.org, **self.campaign_values(**values))

    def running_campaign(self, **values):
        campaign = services.submit_campaign(self.owner, self.make_campaign(**values))
        return services.approve_campaign(self.root, campaign, expected_revision=campaign.revision)


__all__ = ["AdvertisingTestCase", "Campaign", "PASSWORD", "User", "enable_advertisers", "make_user"]
