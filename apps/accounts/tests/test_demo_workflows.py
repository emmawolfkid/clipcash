"""Restricted demo: which workflows the email-verification waiver opens, and which it never does."""

from django.test import override_settings
from django.urls import reverse

from apps.accounts.access import DEMO_UNCONFIRMED_BLOCKED, demo_unconfirmed, email_verification_waived
from apps.advertising import services as advertising
from apps.advertising.models import Organization
from apps.advertising.tests.helpers import AdvertisingTestCase, make_user
from apps.audit.models import AuditEvent
from apps.core import features
from apps.memberships import services as memberships
from apps.memberships.access import check_access
from apps.memberships.models import Subscription
from apps.memberships.services import member_eligibility
from apps.payments import services as payments
from apps.payments.models import PaymentIntent
from apps.payments.providers import sandbox_active
from apps.viewing.models import WatchSession
from apps.wallets import wallet_services
from apps.wallets.models import JournalEntry, Wallet
from apps.withdrawals import services as withdrawals
from apps.withdrawals.models import DemoWithdrawal, PayoutBeneficiary

DEMO_EMAIL = "demo@example.com"
DEMO = override_settings(RESTRICTED_DEMO_MODE=True, DEMO_EMAIL_VERIFICATION_OPTIONAL=True,
                         DEMO_ACCOUNTS=[DEMO_EMAIL], PAYMENTS_MODE="disabled", PAYMENTS_SANDBOX_ALLOWED=False)
NOTICE = "Restricted demonstration account."


@DEMO
class DemoWorkflowTestCase(AdvertisingTestCase):
    """Memberships, videos and advertisers all enabled, with a running campaign."""

    def setUp(self):
        super().setUp()
        self.campaign = self.running_campaign()
        self.demo = make_user(DEMO_EMAIL, verified=False)
        self.client.force_login(self.demo)

    def records(self):
        return {model.__name__: model.objects.count() for model in (
            Subscription, Wallet, JournalEntry, PaymentIntent, PayoutBeneficiary, DemoWithdrawal,
            Organization, WatchSession)}


class OpenWorkflowTests(DemoWorkflowTestCase):
    def test_browsing_pages_open(self):
        for url in (reverse("dashboard"), reverse("activity"), reverse("accounts:profile"),
                    reverse("memberships:plans"), reverse("memberships:plan_detail", args=[self.plan.code]),
                    reverse("memberships:mine"), reverse("videos:library"),
                    reverse("videos:detail", args=[self.ad_video.slug]), reverse("viewing:list"),
                    reverse("viewing:history"), reverse("viewing:campaign", args=[self.campaign.pk]),
                    reverse("rewards:history"), reverse("wallets:wallet"), reverse("withdrawals:overview"),
                    reverse("withdrawals:destinations"), reverse("advertising:home")):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_access_decisions_give_the_real_reason(self):
        self.assertEqual(check_access(self.demo).reason, "no_membership")  # Not "email_unverified".
        page = self.client.get(reverse("videos:detail", args=[self.ad_video.slug]))
        self.assertEqual(page.context["playback_reason"], "no_membership")
        self.assertNotContains(page, reverse("accounts:verification_required"))

    def test_blocked_workflows_are_explained_not_offered(self):
        pages = {
            reverse("dashboard"): None,
            reverse("wallets:wallet"): "Open my wallet",
            reverse("withdrawals:destinations"): "Save destination",
            reverse("memberships:plan_detail", args=[self.plan.code]): "Choose this plan",
            reverse("advertising:home"): "Apply for an advertiser account",
        }
        for url, hidden_action in pages.items():
            with self.subTest(url=url):
                page = self.client.get(url)
                self.assertContains(page, NOTICE)
                if hidden_action:
                    self.assertNotContains(page, hidden_action)

    def test_email_stays_unverified(self):
        self.client.get(reverse("dashboard"))
        self.client.post(reverse("wallets:wallet"))
        self.demo.refresh_from_db()
        self.assertFalse(self.demo.email_verified)


class BlockedWorkflowTests(DemoWorkflowTestCase):
    def assert_blocked(self, response, redirect_to):
        self.assertRedirects(response, redirect_to, fetch_redirect_response=False)
        self.assertIn(str(DEMO_UNCONFIRMED_BLOCKED), [str(m) for m in response.wsgi_request._messages])

    def test_choosing_a_membership(self):
        before = self.records()
        self.assert_blocked(self.client.post(reverse("memberships:choose", args=[self.plan.code])),
                            reverse("memberships:mine"))
        self.assertEqual(self.records(), before)

    def test_opening_a_wallet(self):
        before = self.records()
        self.assert_blocked(self.client.post(reverse("wallets:wallet")), reverse("wallets:wallet"))
        self.assertEqual(self.records(), before)

    def test_adding_a_payout_destination(self):
        before = self.records()
        response = self.client.post(reverse("withdrawals:destinations"), {
            "action": "add", "kind": "MOBILE_MONEY", "holder_name": "Demo User",
            "identifier": "+255712345678", "network": "MPESA"})
        self.assert_blocked(response, reverse("withdrawals:destinations"))
        self.assertEqual(self.records(), before)

    def test_advertiser_application(self):
        before = self.records()
        self.assert_blocked(self.client.get(reverse("advertising:onboard")), reverse("advertising:home"))
        response = self.client.post(reverse("advertising:onboard"), {
            "display_name": "Demo Co", "legal_name": "Demo Co Ltd", "contact_email": "ads@demo.example",
            "country": "TZ", "tax_id": "TIN-1"})
        self.assert_blocked(response, reverse("advertising:home"))
        self.assertEqual(self.records(), before)
        self.assertTrue(AuditEvent.objects.filter(action="advertiser.action.denied").exists())  # Still audited.

    def test_sponsored_viewing_needs_a_membership(self):
        before = self.records()
        response = self.client.post(reverse("viewing:campaign", args=[self.campaign.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("An active membership is needed", [str(m) for m in response.wsgi_request._messages][-1])
        self.assertEqual(self.records(), before)

    def test_financial_and_identity_policies_stay_strict(self):
        self.assertTrue(email_verification_waived(self.demo))
        self.assertEqual(member_eligibility(self.demo), (False, "email_unverified"))  # Default: strict.
        self.assertEqual(wallet_services.wallet_eligibility(self.demo), "email_unverified")
        self.assertFalse(payments._account_ok(self.demo))
        self.assertEqual(advertising.advertiser_account_reason(self.demo), "email_unverified")
        self.assertEqual(list(advertising.pending_invitations_for(self.demo)), [])
        decision = withdrawals.withdrawal_eligibility(self.demo)
        self.assertFalse(decision)
        self.assertIn("withdrawals_locked", decision.reasons)
        self.assertFalse(sandbox_active())
        self.assertEqual([key for key in features.FINANCIAL_FEATURES if features.is_enabled(key)], [])

    def test_membership_service_refuses_before_any_record_exists(self):
        with self.assertRaises(memberships.NotEligible) as raised:
            memberships.choose_plan(self.demo, self.plan)
        self.assertEqual(raised.exception.code, "email_unverified")
        self.assertFalse(Subscription.objects.filter(member=self.demo).exists())


class WaiverOffTests(DemoWorkflowTestCase):
    def test_turning_the_waiver_off_restores_verification_immediately(self):
        for change in ({"DEMO_EMAIL_VERIFICATION_OPTIONAL": False}, {"RESTRICTED_DEMO_MODE": False},
                       {"DEMO_ACCOUNTS": []}):
            with self.subTest(**change), self.settings(**change):
                self.assertFalse(demo_unconfirmed(self.demo))
                self.assertEqual(check_access(self.demo).reason, "email_unverified")
                self.assertRedirects(self.client.get(reverse("wallets:wallet")),
                                     reverse("accounts:verification_required"))
                self.assertRedirects(self.client.get(reverse("advertising:home")),
                                     reverse("accounts:verification_required"))

    def test_unlisted_accounts_get_no_waiver_anywhere(self):
        other = make_user("other@example.com", verified=False)
        self.client.force_login(other)
        self.assertFalse(demo_unconfirmed(other))
        self.assertEqual(check_access(other).reason, "email_unverified")
        self.assertRedirects(self.client.get(reverse("dashboard")), reverse("accounts:verification_required"))

    def test_verified_members_are_unaffected(self):
        self.client.force_login(self.member)  # Verified, not on the list.
        self.assertFalse(demo_unconfirmed(self.member))
        self.assertNotContains(self.client.get(reverse("dashboard")), NOTICE)
        self.client.post(reverse("wallets:wallet"))
        self.assertTrue(Wallet.objects.filter(member=self.member).exists())
