"""PAY-LIVE-01C: one purchase state drives every payment message and the purchase endpoints.

States (apps.payments.manual.purchase_state): open (manual payments on, a method published),
unpublished (on, nothing published), closed (off) and sandbox (isolated test deployments).
Members see exactly one explanation per state; staff see the members' state on the plan page,
told apart from automatic gateways, which stay locked.
"""

from django.test import override_settings
from django.urls import reverse

from apps.core import features
from apps.memberships.access import check_access
from apps.memberships.models import Subscription
from apps.memberships.views import purchase_open
from apps.payments import manual
from apps.payments.models import ManualMembershipPayment

from .manual_helpers import BUSINESS_NUMBER, RECIPIENT, ManualPaymentTestCase, airtel_reference

CLOSED = str(manual.PURCHASE_CLOSED_MESSAGE)
OPEN_PHRASE = "To buy a membership, choose a plan, pay its exact price"
SANDBOX_PHRASE = "Online payment is not available yet"
# Wording that used to appear next to "not open" and contradicted it.
CONTRADICTIONS = ("Choosing a plan saves it as pending", "Payments are temporarily unavailable",
                  "has not published its payment methods", "Membership payments are not available at the moment")
MANUAL_OFF = override_settings(MANUAL_MEMBERSHIP_PAYMENTS_ENABLED=False)
SANDBOX = override_settings(MANUAL_MEMBERSHIP_PAYMENTS_ENABLED=False, PAYMENTS_MODE="sandbox",
                            PAYMENTS_SANDBOX_ALLOWED=True)


class PurchaseStateTests(ManualPaymentTestCase):
    def test_open_when_enabled_and_a_method_is_published(self):
        self.assertEqual(manual.purchase_state(), manual.PURCHASE_OPEN)
        self.assertTrue(purchase_open())

    def test_unpublished_when_enabled_without_a_method(self):
        manual.retire_instruction(self.root, "AIRTEL_LIPA")
        self.assertEqual(manual.purchase_state(), manual.PURCHASE_UNPUBLISHED)
        self.assertFalse(purchase_open())

    @MANUAL_OFF
    def test_closed_when_manual_payments_are_off(self):
        self.assertEqual(manual.purchase_state(), manual.PURCHASE_CLOSED)
        self.assertFalse(purchase_open())

    @override_settings(RESTRICTED_DEMO_MODE=True)
    def test_closed_in_a_restricted_demo_even_when_switched_on(self):
        self.assertEqual(manual.purchase_state(), manual.PURCHASE_CLOSED)

    @SANDBOX
    def test_sandbox_state_only_in_an_isolated_sandbox(self):
        self.assertEqual(manual.purchase_state(), manual.PURCHASE_SANDBOX)
        self.assertTrue(purchase_open())

    def test_financial_locks_are_unchanged(self):
        for key in ("live_payments", "real_money_rewards", "withdrawals"):
            with self.subTest(feature=key):
                self.assertFalse(features.is_enabled(key))


class MemberMessageTests(ManualPaymentTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.member)
        self.plans = reverse("memberships:plans")
        self.detail = reverse("memberships:plan_detail", args=[self.plan.code])
        self.choose_url = reverse("memberships:choose", args=[self.plan.code])

    def assert_one_closed_explanation(self, response, status_code=200):
        self.assertContains(response, CLOSED, count=1, status_code=status_code)
        self.assertNotContains(response, OPEN_PHRASE, status_code=status_code)
        for phrase in CONTRADICTIONS + (SANDBOX_PHRASE, "Choose this plan", "Pay now"):
            self.assertNotContains(response, phrase, status_code=status_code)

    # --- open ------------------------------------------------------------------------

    def test_open_shows_the_published_method_and_offers_choosing(self):
        for url in (self.plans, self.detail):
            with self.subTest(url=url):
                page = self.client.get(url)
                self.assertContains(page, OPEN_PHRASE, count=1)
                self.assertContains(page, "Airtel Money - Lipa")
                self.assertNotContains(page, CLOSED)
                for phrase in CONTRADICTIONS + (SANDBOX_PHRASE,):
                    self.assertNotContains(page, phrase)
        self.assertContains(self.client.get(self.detail), "Choose this plan")

    def test_open_checkout_shows_the_exact_airtel_lipa_details(self):
        response = self.client.post(self.choose_url)
        subscription = Subscription.objects.get(member=self.member)
        self.assertRedirects(response, reverse("payments:manual_pay", args=[subscription.pk]))
        page = self.client.get(reverse("payments:manual_pay", args=[subscription.pk]))
        self.assertContains(page, "Airtel Money - Lipa")
        self.assertContains(page, BUSINESS_NUMBER)
        self.assertContains(page, RECIPIENT)
        self.assertContains(page, "TZS 5,000")
        self.assertContains(page, "Dial *150*60#.")
        self.assertNotContains(page, CLOSED)

    # --- closed and unpublished: one identical explanation ----------------------------

    def check_closed_pages(self):
        for url in (self.plans, self.detail):
            with self.subTest(url=url):
                self.assert_one_closed_explanation(self.client.get(url))
        response = self.client.post(self.choose_url)
        self.assertRedirects(response, self.detail, fetch_redirect_response=False)
        self.assertEqual([str(m) for m in response.wsgi_request._messages], [CLOSED])
        self.assertFalse(Subscription.objects.filter(member=self.member).exists())

    @MANUAL_OFF
    def test_closed_explains_once_and_blocks_choosing(self):
        self.check_closed_pages()

    def test_unpublished_gives_the_same_explanation_and_blocks_choosing(self):
        manual.retire_instruction(self.root, "AIRTEL_LIPA")
        self.check_closed_pages()

    def test_existing_choice_cannot_be_paid_while_closed(self):
        subscription = self.choose()  # Chosen while payments were open.
        pay_url = reverse("payments:manual_pay", args=[subscription.pk])
        with MANUAL_OFF:
            self.assert_one_closed_explanation(self.client.get(reverse("memberships:mine")))
            detail = self.client.get(reverse("memberships:subscription_detail", args=[subscription.pk]))
            self.assert_one_closed_explanation(detail)
            page = self.client.get(pay_url)
            self.assert_one_closed_explanation(page, status_code=503)
            self.assertNotContains(page, BUSINESS_NUMBER, status_code=503)
            posted = self.client.post(pay_url, {"amount": "5000", "payer_phone": "0781234567",
                                                "transaction_reference": airtel_reference()})
            self.assertEqual(posted.status_code, 503)
            with self.assertRaises(manual.Unavailable):
                manual.submit_claim(self.member, subscription, amount=5000, payer_phone="0781234567",
                                    transaction_reference=airtel_reference())
        self.assertFalse(ManualMembershipPayment.objects.exists())

    @MANUAL_OFF
    def test_dashboard_uses_the_same_explanation(self):
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, CLOSED)
        for phrase in CONTRADICTIONS:
            self.assertNotContains(page, phrase)

    # --- sandbox (isolated test deployments) -----------------------------------------

    @SANDBOX
    def test_sandbox_keeps_its_own_single_notice(self):
        page = self.client.get(self.detail)
        self.assertContains(page, SANDBOX_PHRASE, count=1)
        self.assertNotContains(page, CLOSED)
        self.assertNotContains(page, OPEN_PHRASE)


class AdminMessageTests(ManualPaymentTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.root)
        self.url = reverse("administration:plan", args=[self.plan.pk])

    def page(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Automatic payment gateways are not used")
        for internal in ("DJANGO_", "MANUAL_MEMBERSHIP_PAYMENTS", "PAYMENTS_MODE"):
            self.assertNotContains(response, internal)
        return response

    def test_open_is_not_reported_as_disabled(self):
        page = self.page()
        self.assertContains(page, "Open: members can buy active, listed plans with a manual payment.")
        self.assertNotContains(page, "Payments disabled")
        self.assertNotContains(page, "not offered to members")

    def test_open_but_this_plan_is_not_offered(self):
        from apps.memberships import services as memberships

        memberships.set_plan_status(self.root, self.plan, "INACTIVE")
        self.assertContains(self.page(), "This plan is not offered to members because it is inactive or not listed.")

    def test_unpublished_says_what_is_missing(self):
        manual.retire_instruction(self.root, "AIRTEL_LIPA")
        page = self.page()
        self.assertContains(page, "no payment method is published")
        self.assertNotContains(page, "Payments disabled")

    @MANUAL_OFF
    def test_closed_is_reported_as_disabled(self):
        page = self.page()
        self.assertContains(page, "Payments disabled: manual membership payments are not switched on")
        self.assertNotContains(page, "Open: members can buy")


class VerificationUnchangedTests(ManualPaymentTestCase):
    def test_submission_never_activates_and_approval_activates_once(self):
        claim = self.submit()
        self.assertEqual(claim.status, ManualMembershipPayment.Status.PENDING_VERIFICATION)
        self.assertEqual(claim.subscription.status, Subscription.Status.PENDING)
        self.assertEqual(check_access(self.member).reason, "no_membership")
        approved, changed = self.approve(claim)
        self.assertTrue(changed)
        self.assertEqual(check_access(self.member).reason, "ok")
        again, changed_again = self.approve(approved)
        self.assertFalse(changed_again)
        self.assertEqual(Subscription.objects.filter(member=self.member, status=Subscription.Status.ACTIVE).count(), 1)

    def test_verification_still_needs_manual_payments_on(self):
        claim = self.submit()
        with MANUAL_OFF, self.assertRaises(manual.Unavailable):
            self.approve(claim)
        claim.refresh_from_db()
        self.assertEqual(claim.status, ManualMembershipPayment.Status.PENDING_VERIFICATION)
