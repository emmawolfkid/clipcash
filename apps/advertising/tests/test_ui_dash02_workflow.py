"""UI-DASH-02 end-to-end advertising workflow through the real HTTP views.

Roles: Super Administrator (console), advertiser owner, advertiser manager, an eligible member
and ineligible members. Covers application review, video authorization, campaign creation and
approval, delivery rules (draft, submitted, paused, expired, plan-targeted), verified viewing,
duplicate-view refusal, advertiser and console reporting, organization isolation and Swahili.
Nothing is bypassed: every step uses the same views and services as the application.
"""
from datetime import timedelta

from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.advertising.models import AdvertiserMembership, Campaign, Organization
from apps.advertising import services as advertising
from apps.viewing.models import WatchSession
from apps.viewing.tests.helpers import ViewingTestCase, make_user

FORMAT = "%Y-%m-%dT%H:%M"


@override_settings(VIEWING_MAX_STARTS_PER_HOUR=100)
class AdvertisingWorkflowTests(ViewingTestCase):
    def setUp(self):
        super().setUp()
        # A second, unrelated advertiser and a library video staff will authorize for the new one.
        self.creative = self.published_video("Fresh juice summer clip")

    # --- helpers -------------------------------------------------------------------------------
    def as_user(self, user, language="en"):
        self.client.force_login(user)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language

    def apply_as_advertiser(self, owner):
        self.as_user(owner)
        response = self.client.post(reverse("advertising:onboard"), {
            "display_name": "Juice Co", "legal_name": "Juice Company Limited", "contact_email": "ads@juice.example",
            "country": "TZ", "tax_id": "TIN-555-555-555"})
        organization = Organization.objects.get(display_name="Juice Co")
        self.assertRedirects(response, reverse("advertising:organization", args=[organization.pk]))
        return organization

    def campaign_form(self, **values):
        now = timezone.now()
        data = {"name": "Summer juice", "description": "Cold and fresh.", "video": self.creative.pk,
                "starts_at": (now - timedelta(hours=1)).strftime(FORMAT),
                "ends_at": (now + timedelta(days=7)).strftime(FORMAT),
                "max_views_per_member": 1, "demo_budget_amount": 50_000, "demo_reward_per_view": 100}
        data.update(values)
        return data

    def offers_for(self, member):
        self.as_user(member)
        return self.client.get(reverse("viewing:list")).content.decode()

    # --- the workflow ---------------------------------------------------------------------------
    def test_application_to_verified_view_to_reports(self):
        owner = make_user("owner@juice.example")
        organization = self.apply_as_advertiser(owner)
        self.assertEqual(organization.verification_status, Organization.Verification.PENDING)

        # 1. Administrator approves the advertiser and authorizes a video creative (console views).
        self.as_user(self.root)
        review = reverse("administration:advertiser_review", args=[organization.pk])
        self.assertEqual(self.client.get(review).status_code, 200)
        self.client.post(review, {"action": "approve", "expected_status": "PENDING"})
        self.client.post(review, {"action": "grant", "video": self.creative.pk})
        organization.refresh_from_db()
        self.assertEqual(organization.verification_status, Organization.Verification.APPROVED)
        self.assertTrue(advertising.video_authorized(organization, self.creative))

        # 2. The owner sees the creative and creates a draft campaign (advertiser views).
        self.as_user(owner)
        creatives = self.client.get(reverse("advertising:creatives", args=[organization.pk]))
        self.assertContains(creatives, "Fresh juice summer clip")
        response = self.client.post(reverse("advertising:campaign_create", args=[organization.pk]), self.campaign_form())
        campaign = Campaign.objects.get(organization=organization, name="Summer juice")
        self.assertRedirects(response, reverse("advertising:campaign", args=[organization.pk, campaign.pk]))
        self.assertEqual(campaign.status, Campaign.Status.DRAFT)

        # Drafts are never delivered.
        self.assertNotIn("Summer juice", self.offers_for(self.member))

        # 3. Submit for review; still not delivered while awaiting review.
        self.as_user(owner)
        self.client.post(reverse("advertising:campaign_action", args=[organization.pk, campaign.pk, "submit"]))
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.SUBMITTED)
        self.assertNotIn("Summer juice", self.offers_for(self.member))

        # 4. Administrator approves (revision check) from the campaign review page.
        self.as_user(self.root)
        self.assertContains(self.client.get(reverse("administration:campaigns")), "Summer juice")
        self.client.post(reverse("administration:campaign_review", args=[campaign.pk]),
                         {"action": "approve", "expected_revision": campaign.revision})
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.ACTIVE)

        # 5. Eligible member discovers the sponsored video on the dashboard and the sponsored page.
        page = self.offers_for(self.member)
        self.assertIn("Summer juice", page)
        self.assertIn("Juice Co", page)
        dashboard = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('class="ad-card"', dashboard)
        self.assertIn("Summer juice", dashboard)
        self.assertIn(reverse("viewing:campaign", args=[campaign.pk]), dashboard)

        # Ineligible: no membership -> not offered for watching; the start is refused server-side.
        outsider = make_user("no.plan@example.com")
        self.as_user(outsider)
        self.assertNotContains(self.client.get(reverse("dashboard")), 'class="ad-card"')
        self.client.post(reverse("viewing:campaign", args=[campaign.pk]))
        self.assertFalse(WatchSession.objects.filter(member=outsider).exists())
        # The advertiser's own team cannot watch their campaign for credit.
        self.as_user(owner)
        self.client.post(reverse("viewing:campaign", args=[campaign.pk]))
        self.assertFalse(WatchSession.objects.filter(member=owner).exists())

        # 6. The eligible member watches through the real session flow and the view is verified.
        self.as_user(self.member)
        response = self.client.post(reverse("viewing:campaign", args=[campaign.pk]))
        session = WatchSession.objects.get(member=self.member, campaign=campaign)
        self.assertRedirects(response, reverse("viewing:session", args=[session.pk]), fetch_redirect_response=False)
        self.event(session, 1, 0)
        position, seq = 0, 1
        while position < 95:
            seq += 1
            position = min(95, position + 10)
            self.event(session, seq, position, after=10)
        self.event(session, seq + 1, 95, state="ended", after=1)
        session.refresh_from_db()
        self.assertEqual(session.outcome, WatchSession.Outcome.VERIFIED_EVIDENCE)

        # 7. Duplicate viewing is refused (max one verified view per member for this campaign).
        self.client.post(reverse("viewing:campaign", args=[campaign.pk]))
        self.assertEqual(WatchSession.objects.filter(member=self.member, campaign=campaign).count(), 1)

        # 8. Reports: authoritative figures (masked for the advertiser) and the console view.
        self.assertEqual(advertising.campaign_report(campaign, for_advertiser=False)["viewing"]["verified"]["value"], 1)
        self.as_user(owner)
        for name in ("organization", "reports"):
            self.assertEqual(self.client.get(reverse(f"advertising:{name}", args=[organization.pk])).status_code, 200)
        detail = self.client.get(reverse("advertising:campaign", args=[organization.pk, campaign.pk]))
        self.assertContains(detail, "Verified views")
        self.as_user(self.root)
        self.assertEqual(self.client.get(reverse("administration:campaign_review", args=[campaign.pk])).status_code, 200)

        # 9. Pausing stops delivery; resuming restores it. Expiry ends delivery.
        self.as_user(owner)
        self.client.post(reverse("advertising:campaign_action", args=[organization.pk, campaign.pk, "pause"]))
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.PAUSED)
        other = make_user("second.member@example.com")
        self.active_membership(member=other)
        self.assertNotIn("Summer juice", self.offers_for(other))
        self.as_user(owner)
        self.client.post(reverse("advertising:campaign_action", args=[organization.pk, campaign.pk, "resume"]))
        self.assertIn("Summer juice", self.offers_for(other))
        self.clock.advance(8 * 24 * 3600)  # Past ends_at.
        self.assertNotIn("Summer juice", self.offers_for(other))

    def test_plan_targeting_locks_other_plans(self):
        gold = self.make_plan("gold", price=20_000, days=30)
        campaign = self.running_campaign(name="Gold only", target_plans=[gold])
        page = self.offers_for(self.member)  # self.member holds the "monthly" plan.
        self.assertIn("Gold only", page)
        self.assertIn("video-card--locked", page)
        self.assertNotIn(f'href="{reverse("viewing:campaign", args=[campaign.pk])}"><div', page.replace("\n", ""))
        self.client.post(reverse("viewing:campaign", args=[campaign.pk]))
        self.assertFalse(WatchSession.objects.filter(member=self.member, campaign=campaign).exists())

    def test_manager_role_and_organization_isolation(self):
        # The fixture's approved advertiser ("Brand") invites a manager.
        manager = make_user("manager@brand.example")
        invitation = advertising.invite_member(self.owner, self.org, "manager@brand.example",
                                               AdvertiserMembership.Role.MANAGER)
        advertising.accept_invitation(manager, invitation.pk)
        campaign = self.make_campaign(name="Brand draft")
        self.as_user(manager)
        for name in ("organization", "campaigns", "creatives", "reports"):
            self.assertEqual(self.client.get(reverse(f"advertising:{name}", args=[self.org.pk])).status_code, 200, name)
        self.assertContains(self.client.get(reverse("advertising:campaign", args=[self.org.pk, campaign.pk])),
                            "Brand draft")
        # Team management stays with owners (the service refuses managers).
        self.client.post(reverse("advertising:team", args=[self.org.pk]), {"email": "x@brand.example", "role": "MANAGER"})
        self.assertFalse(self.org.invitations.filter(email="x@brand.example").exists())

        # Another advertiser cannot reach this organization's pages or campaigns (404, not 403).
        rival = make_user("owner@rival.example")
        rival_org = advertising.create_organization(rival, display_name="Rival", legal_name="Rival Ltd",
                                                    contact_email="ads@rival.example", country="TZ")
        self.as_user(rival)
        for path in (reverse("advertising:organization", args=[self.org.pk]),
                     reverse("advertising:reports", args=[self.org.pk]),
                     reverse("advertising:creatives", args=[self.org.pk]),
                     reverse("advertising:campaign", args=[self.org.pk, campaign.pk]),
                     reverse("advertising:campaign", args=[rival_org.pk, campaign.pk]),
                     reverse("advertising:campaigns", args=[self.org.pk]) + f"?video={self.ad_video.pk}"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        own = self.client.get(reverse("advertising:campaigns", args=[rival_org.pk]) + f"?video={self.ad_video.pk}")
        self.assertNotContains(own, "Brand draft")

    def test_advertiser_workspace_in_swahili(self):
        self.make_campaign(name="Brand draft")
        self.as_user(self.owner, language="sw")
        page = self.client.get(reverse("advertising:organization", args=[self.org.pk]))
        for text in ("Muhtasari", "Menyu ya mtangazaji", "Jumla ya kampeni", "Video za matangazo",
                     "Dashibodi ya mwanachama", "Utendaji wa kampeni"):
            self.assertContains(page, text)
        self.assertContains(page, "Brand draft")  # User content is never translated.
        creatives = self.client.get(reverse("advertising:creatives", args=[self.org.pk]))
        self.assertContains(creatives, "Inatumika katika kampeni")
        self.assertNotContains(page, 'aria-label="Member menu"')

    def test_sidebar_is_advertiser_specific(self):
        self.as_user(self.owner)
        page = self.client.get(reverse("advertising:organization", args=[self.org.pk])).content.decode()
        sidebar = page.split('class="app-sidebar app-sidebar--advertiser"', 1)[1].split("</aside>", 1)[0]
        for route in ("advertising:organization", "advertising:campaigns", "advertising:campaign_create",
                      "advertising:creatives", "advertising:reports", "advertising:team", "advertising:profile"):
            self.assertIn(f'href="{reverse(route, args=[self.org.pk])}"', sidebar, route)
        for member_only in (reverse("wallets:wallet"), reverse("memberships:mine"), reverse("withdrawals:overview")):
            self.assertNotIn(f'href="{member_only}"', sidebar)
        self.assertIn(f'href="{reverse("dashboard")}"', sidebar)
        self.assertIn("data-collapsible", sidebar)
