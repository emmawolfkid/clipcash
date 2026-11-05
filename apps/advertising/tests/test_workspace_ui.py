"""UI-06: the advertiser workspace shows real figures, offers only permitted actions and stays structured."""

import re
from datetime import timedelta

from django.conf import settings
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.helpers import make_user
from apps.advertising import services
from apps.advertising.models import Campaign
from apps.advertising.tests.helpers import AdvertisingTestCase
from apps.payments import services as payments
from apps.payments.tests.helpers import SANDBOX, token
from apps.rewards import services as rewards
from apps.rewards.tests.helpers import RewardsTestCase

TEMPLATES = settings.BASE_DIR / "templates"
CSS = settings.BASE_DIR / "static/css"
ADVERTISER_TEMPLATES = ("home", "onboard", "organization", "campaign_list", "campaign_detail", "campaign_form",
                        "reports", "team", "profile")
INVENTED_FIGURES = ("ROI", "Revenue", "Conversion", "Impressions", "Earnings", "Click-through")


def h1s(response):
    return re.findall(r"<h1\b[^>]*>(.*?)</h1>", response.content.decode(), flags=re.S)


class WorkspaceTestCase(AdvertisingTestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_user("manager@brand.example")
        services.accept_invitation(self.manager, services.invite_member(self.owner, self.org, self.manager.email,
                                                                        "MANAGER").pk)
        self.client.force_login(self.owner)

    def org_in_state(self, decision, name):
        user = make_user(f"{name.lower()}@example.com")
        org = self.make_org(user, name)
        if decision == "reject":
            org = services.review_organization(self.root, org, "reject", expected_status="PENDING", reason="Not confirmed")
        elif decision == "suspend":
            org = services.review_organization(self.root, org, "approve", expected_status="PENDING")
            org = services.review_organization(self.root, org, "suspend", expected_status="APPROVED", reason="Review")
        self.client.force_login(user)
        return org

    def url(self, name, *args, org=None):
        return reverse(f"advertising:{name}", args=[(org or self.org).pk, *args])


class PageStructureTests(WorkspaceTestCase):
    def test_every_page_has_one_title_of_its_own_and_the_organization_above_it(self):
        campaign = self.make_campaign(name="Launch draft")
        pages = {"organization": ("Overview",), "campaigns": ("Campaigns",), "reports": ("Reports",),
                 "team": ("Team",), "profile": ("Profile",), "campaign_create": ("New campaign",)}
        for name, (title,) in pages.items():
            with self.subTest(page=name):
                response = self.client.get(self.url(name))
                self.assertEqual(h1s(response), [title])
                self.assertContains(response, 'class="workspace-org__name">Brand<')
        self.assertEqual(h1s(self.client.get(self.url("campaign", campaign.pk))), ["Launch draft"])
        self.assertEqual(h1s(self.client.get(self.url("campaign_edit", campaign.pk))), ["Edit campaign"])

    def test_current_tab_is_marked_for_assistive_technology(self):
        response = self.client.get(self.url("reports"))
        self.assertContains(response, f'aria-current="page" href="{self.url("reports")}"')
        self.assertContains(response, 'aria-label="Advertiser sections"')

    def test_tables_have_captions_and_column_scopes(self):
        self.make_campaign()
        for name in ("organization", "campaigns", "reports", "team"):
            html = self.client.get(self.url(name)).content.decode()
            for table in re.findall(r"<table.*?</table>", html, flags=re.S):
                with self.subTest(page=name):
                    self.assertIn("<caption", table)
                    self.assertNotRegex(table, r"<th(?=[\s>])(?![^>]*scope=)")

    def test_other_advertisers_still_get_not_found(self):
        rival = make_user("rival@example.com")
        self.client.force_login(rival)
        for name in ("organization", "campaigns", "reports", "team", "profile"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(self.url(name)).status_code, 404)


class OrganizationStateTests(WorkspaceTestCase):
    def test_new_campaign_is_offered_only_where_the_service_allows_it(self):
        self.assertContains(self.client.get(self.url("campaigns")), self.url("campaign_create"))
        pending = self.org_in_state("none", "Waiting")
        self.assertContains(self.client.get(self.url("campaigns", org=pending)), self.url("campaign_create", org=pending))
        self.assertContains(self.client.get(self.url("organization", org=pending)), "awaiting review")
        for decision, name, message in (("reject", "Declined", "Not confirmed"), ("suspend", "Held", "is suspended")):
            org = self.org_in_state(decision, name)
            for page in ("organization", "campaigns"):
                with self.subTest(state=decision, page=page):
                    response = self.client.get(self.url(page, org=org))
                    self.assertNotContains(response, self.url("campaign_create", org=org))
                    self.assertContains(response, message)
                    self.assertNotContains(response, "Create a draft")

    def test_create_page_explains_instead_of_showing_a_form_that_would_be_refused(self):
        org = self.org_in_state("suspend", "Held")
        page = self.client.get(self.url("campaign_create", org=org))
        self.assertContains(page, "not in good standing")
        self.assertNotContains(page, "Save draft")
        self.client.post(self.url("campaign_create", org=org), {"name": "Nope"})
        self.assertFalse(Campaign.objects.filter(organization=org).exists())

    def test_no_video_yet_is_explained(self):
        org = self.org_in_state("none", "Fresh")
        self.assertContains(self.client.get(self.url("campaign_create", org=org)), "has not authorized any videos")

    def test_home_lists_advertisers_and_applicants_get_one_clear_action(self):
        home = self.client.get(reverse("advertising:home"))
        self.assertContains(home, "Your advertisers")
        self.assertContains(home, self.url("organization"))
        self.client.force_login(make_user("nobody@example.com"))
        self.assertContains(self.client.get(reverse("advertising:home")), "Apply for an advertiser account")


class ProfileAndTeamTests(WorkspaceTestCase):
    def test_owner_edits_grouped_fields_and_verified_details_are_read_only(self):
        page = self.client.get(self.url("profile"))
        for text in ("Review status", "Public identity", "Legal and registration", "they are read-only here"):
            self.assertContains(page, text)
        self.assertRegex(page.content.decode(), r'name="legal_name"[^>]*\bdisabled')
        self.assertNotRegex(page.content.decode(), r'name="display_name"[^>]*\bdisabled')
        self.assertContains(page, "Save profile")

    def test_manager_reads_the_profile_without_a_form(self):
        self.client.force_login(self.manager)
        page = self.client.get(self.url("profile"))
        self.assertContains(page, "Only owners can change the profile")
        self.assertNotContains(page, "Save profile")

    def test_rejected_owner_can_resubmit_and_suspended_owner_only_reads(self):
        rejected = self.org_in_state("reject", "Declined")
        self.assertContains(self.client.get(self.url("profile", org=rejected)), "Resubmit for review")
        suspended = self.org_in_state("suspend", "Held")
        page = self.client.get(self.url("profile", org=suspended))
        self.assertContains(page, "cannot be changed while this advertiser is suspended")
        self.assertNotContains(page, "Save profile")

    def test_owner_gets_confirmed_removal_and_the_invitation_form(self):
        page = self.client.get(self.url("team"))
        self.assertContains(page, "Confirm removal")
        self.assertContains(page, "Confirm leaving")
        self.assertContains(page, "Invite a team member")
        self.assertContains(page, "csrfmiddlewaretoken")

    def test_manager_can_only_leave_and_the_service_still_enforces_it(self):
        owner_membership = self.org.memberships.get(user=self.owner)
        self.client.force_login(self.manager)
        page = self.client.get(self.url("team"))
        self.assertContains(page, "Only owners can invite or remove other people")
        self.assertContains(page, "Confirm leaving")
        self.assertNotContains(page, "Confirm removal")
        self.assertNotContains(page, "Invite a team member")
        response = self.client.post(reverse("advertising:team_change", args=[self.org.pk, "remove", owner_membership.pk]))
        self.assertEqual(response.status_code, 403)
        owner_membership.refresh_from_db()
        self.assertTrue(owner_membership.is_active)

    def test_last_owner_keeps_the_existing_error_feedback(self):
        membership = self.org.memberships.get(user=self.owner)
        response = self.client.post(reverse("advertising:team_change", args=[self.org.pk, "remove", membership.pk]),
                                    follow=True)
        self.assertContains(response, "must keep at least one owner")


class CampaignPresentationTests(WorkspaceTestCase):
    def test_list_shows_video_audience_and_formatted_demo_budget(self):
        campaign = self.make_campaign(name="Targeted")
        campaign.target_plans.set([self.plan])
        self.make_campaign(name="Everyone")
        page = self.client.get(self.url("campaigns"))
        for text in ("Brand clip", self.plan.name, "All active members", "TZS 100,000", "Demo budget (not money)"):
            self.assertContains(page, text)

    def test_empty_and_filtered_lists_say_different_things(self):
        self.assertContains(self.client.get(self.url("campaigns")), "No campaigns yet")
        self.make_campaign()
        filtered = self.client.get(self.url("campaigns"), {"q": "zzzz"})
        self.assertContains(filtered, "No campaigns match these filters")
        self.assertContains(filtered, "Clear filters")

    def test_changes_requested_is_a_warning_with_the_review_note_not_a_failure(self):
        campaign = services.submit_campaign(self.owner, self.make_campaign())
        campaign = services.reject_campaign(self.root, campaign, expected_revision=campaign.revision,
                                            reason="Remove the price claim.")
        page = self.client.get(self.url("campaign", campaign.pk))
        self.assertContains(page, "Remove the price claim.")
        self.assertContains(page, 'class="badge badge--paused">Changes requested')
        self.assertNotContains(page, "badge--rejected")
        self.assertContains(page, "Edit the campaign, then submit it for review again")

    def test_unapproved_advertiser_cannot_submit_and_is_told_why(self):
        user = make_user("waiting@example.com")
        org = self.make_org(user, "Waiting")
        services.grant_video(self.root, org, self.ad_video)
        campaign = self.make_campaign(user=user, org=org)
        self.client.force_login(user)
        page = self.client.get(self.url("campaign", campaign.pk, org=org))
        self.assertContains(page, 'disabled aria-describedby="submit-help"')
        self.assertContains(page, "once your advertiser account is approved")
        self.client.post(self.url("campaign_action", campaign.pk, "submit", org=org))
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "DRAFT")

    def test_staff_paused_campaign_cannot_be_resumed_from_here(self):
        campaign = services.pause_campaign(self.root, self.running_campaign(), reason="Under review")
        page = self.client.get(self.url("campaign", campaign.pk))
        self.assertContains(page, "Paused by ClipCash staff")
        self.assertNotContains(page, ">Resume<")

    def test_permanent_actions_keep_their_confirmation(self):
        campaign = self.make_campaign()
        page = self.client.get(self.url("campaign", campaign.pk))
        self.assertContains(page, "Cancelling is permanent")
        self.client.post(self.url("campaign_action", campaign.pk, "cancel"))
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "DRAFT")

    def test_campaign_that_has_not_run_shows_no_results_instead_of_zeros(self):
        now = timezone.now()
        campaign = self.make_campaign(starts_at=now + timedelta(days=2), ends_at=now + timedelta(days=9))
        page = self.client.get(self.url("campaign", campaign.pk))
        self.assertContains(page, "this campaign has not run")
        self.assertNotContains(page, "Awaiting staff review")

    def test_form_groups_fields_and_preserves_the_edit_contract(self):
        campaign = self.make_campaign()
        page = self.client.get(self.url("campaign_edit", campaign.pk))
        for text in ("Campaign details", "Schedule", "Audience", "Demo planning figures",
                     f'name="revision" value="{campaign.revision}"', "csrfmiddlewaretoken"):
            self.assertContains(page, text)
        for field in ("name", "description", "video", "starts_at", "ends_at", "target_plans", "max_views_per_member",
                      "demo_budget_amount", "demo_reward_per_view"):
            self.assertContains(page, f'name="{field}"')

    def test_invalid_form_shows_a_linked_error_summary_and_saves_nothing(self):
        response = self.client.post(self.url("campaign_create"), {"name": ""})
        self.assertContains(response, 'class="error-summary"')
        self.assertContains(response, 'href="#id_name"')
        self.assertFalse(Campaign.objects.exists())


class OverviewAndReportFigures(WorkspaceTestCase):
    def test_overview_links_counts_to_the_existing_status_filter(self):
        self.make_campaign()
        page = self.client.get(self.url("organization"))
        self.assertContains(page, f'{self.url("campaigns")}?status=DRAFT')
        self.assertContains(page, "Spend: not applicable")
        self.assertNotContains(page, f'{self.url("campaigns")}?status=ARCHIVED')

    def test_overview_for_no_campaigns_has_an_empty_state(self):
        page = self.client.get(self.url("organization"))
        self.assertContains(page, "No campaigns yet. Create a draft to get started.")
        self.assertNotContains(page, "Campaigns by status")

    def test_no_invented_commercial_figures_or_export(self):
        campaign = self.make_campaign()
        for path in (self.url("organization"), self.url("campaigns"), self.url("reports"),
                     self.url("campaign", campaign.pk)):
            page = self.client.get(path)
            for invented in INVENTED_FIGURES:
                with self.subTest(path=path, figure=invented):
                    self.assertNotContains(page, invented)
        self.assertNotContains(self.client.get(self.url("reports")), "export")

    def test_report_distinguishes_not_run_from_zero(self):
        now = timezone.now()
        self.make_campaign(name="Future", starts_at=now + timedelta(days=2), ends_at=now + timedelta(days=9))
        page = self.client.get(self.url("reports"))
        self.assertContains(page, "Has not run")
        self.assertContains(page, "No viewing sessions started in this period")
        self.assertContains(page, "Results by campaign")

    def test_report_with_no_campaigns_has_an_empty_state(self):
        self.assertContains(self.client.get(self.url("reports")), "No campaigns yet.")


@override_settings(VIEWING_MAX_STARTS_PER_HOUR=100)
class VerifiedFigureTests(RewardsTestCase):
    def test_small_counts_stay_masked_and_demo_figures_stay_labelled(self):
        self.approved_policy(amount=70)
        self.verified_session()
        rewards.evaluate_pending()
        self.client.force_login(self.owner)
        with override_settings(ADVERTISING_REPORT_MIN_COUNT=5):
            detail = self.client.get(reverse("advertising:campaign", args=[self.org.pk, self.campaign.pk]))
            report = self.client.get(reverse("advertising:reports", args=[self.org.pk]))
        self.assertContains(detail, "fewer than 5")
        self.assertContains(report, "fewer than 5")
        self.assertContains(detail, "Demo rewards (simulated, not money)")
        self.assertContains(detail, "Hidden for privacy")

    @override_settings(ADVERTISING_REPORT_MIN_COUNT=0)
    def test_exact_counts_match_the_report_service(self):
        self.approved_policy(amount=70)
        self.verified_session()
        self.client.force_login(self.owner)
        page = self.client.get(reverse("advertising:reports", args=[self.org.pk]))
        self.assertEqual(page.context["report"]["totals"]["verified"], "1")
        self.assertContains(page, "<dd>1</dd>", html=True)
        self.assertNotContains(page, "Has not run")


@SANDBOX
class FundingPageTests(AdvertisingTestCase):
    def test_funding_uses_the_workspace_structure_and_sandbox_labels(self):
        payments.create_deposit_intent(self.owner, self.org, amount=2_500_000, idempotency_key=token())
        self.client.force_login(self.owner)
        page = self.client.get(reverse("payments:funding", args=[self.org.pk]))
        self.assertEqual(h1s(page), ["Funding (sandbox)"])
        self.assertContains(page, "Settled deposits (sandbox)")
        self.assertContains(page, "SANDBOX: simulated payments")


class ModuleAndMenuTests(SimpleTestCase):
    def test_workspace_templates_avoid_card_components_and_inline_css(self):
        for name in ADVERTISER_TEMPLATES:
            source = (TEMPLATES / f"advertising/{name}.html").read_text(encoding="utf-8")
            with self.subTest(template=name):
                self.assertNotRegex(source, r"\bstyle=|<style")
                self.assertNotRegex(source, r'class="[^"]*\b(stat|panel|card-grid|stat-grid)\b')
                self.assertIn("i18n", source)

    def test_shared_admin_components_were_left_for_the_console(self):
        for name in ("campaign_report", "campaign_performance"):
            self.assertTrue((TEMPLATES / f"components/{name}.html").exists())
            for page in ADVERTISER_TEMPLATES:
                self.assertNotIn(name, (TEMPLATES / f"advertising/{page}.html").read_text(encoding="utf-8"))

    def test_page_modules_are_loaded_only_where_they_are_used(self):
        for name in ADVERTISER_TEMPLATES:
            source = (TEMPLATES / f"advertising/{name}.html").read_text(encoding="utf-8")
            with self.subTest(template=name):
                self.assertEqual("pages/advertising/campaign.css" in source, name == "campaign_detail")

    def test_workspace_table_badges_never_break_mid_word(self):
        css = (CSS / "components/workspace.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.workspace-table \.badge \{\s*white-space: nowrap;")

    def test_one_menu_button_is_shared_with_the_sidebar(self):
        script = (settings.BASE_DIR / "static/js/member-nav.js").read_text(encoding="utf-8")
        self.assertIn("sidebarToggle.hidden = true", script)
        self.assertIn('siteMenu.id + " " + sidebarMenu.id', script)
        self.assertIn(".site-nav .site-nav__duplicate", (CSS / "layouts/public.css").read_text(encoding="utf-8"))
        self.assertIn(".app-sidebar.is-collapsed", (CSS / "layouts/member.css").read_text(encoding="utf-8"))
