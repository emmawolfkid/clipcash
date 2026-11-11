"""PAY-01: member pages and the staff verification console for manual (Airtel Lipa)
membership payments."""

from django.test import Client, override_settings
from django.urls import reverse

from apps.accounts.tests.helpers import PASSWORD, make_user
from apps.audit.models import AuditEvent
from apps.memberships.models import Subscription
from apps.payments import manual
from apps.payments.models import ManualMembershipPayment, ManualPaymentInstruction
from apps.wallets.models import JournalEntry

from .manual_helpers import BUSINESS_NUMBER, RECIPIENT, ManualPaymentTestCase, staff_with

C = ManualMembershipPayment.Status
OBSOLETE_NOTICE = "Online payment is not available yet"


def pay_url(subscription):
    return reverse("payments:manual_pay", args=[subscription.pk])


def claim_url(claim):
    return reverse("payments:manual_claim", args=[claim.pk])


def review_url(claim):
    return reverse("administration:membership_payment", args=[claim.pk])


class MemberJourneyTests(ManualPaymentTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.member)

    def test_plan_pages_describe_the_real_payment_method(self):
        for url in (reverse("memberships:plans"), reverse("memberships:plan_detail", args=["monthly"])):
            response = self.client.get(url)
            self.assertContains(response, "Airtel Money (Lipa)")
            self.assertNotContains(response, OBSOLETE_NOTICE)
            self.assertNotContains(response, "Sandbox")

    def test_choose_pay_submit_and_follow_status(self):
        response = self.client.post(reverse("memberships:choose", args=["monthly"]))
        pending = Subscription.objects.get(member=self.member, status="PENDING")
        self.assertRedirects(response, pay_url(pending))

        page = self.client.get(pay_url(pending))
        self.assertContains(page, "TZS 5,000")
        self.assertContains(page, BUSINESS_NUMBER)
        self.assertContains(page, RECIPIENT)
        self.assertContains(page, "Dial *150*60#.")
        self.assertContains(page, "does not activate your membership by itself")
        self.assertContains(page, "css/pages/payments/manual.css")
        self.assertNotContains(page, "style=")

        response = self.client.post(pay_url(pending), {"amount": "5,000", "payer_phone": "0781234567",
                                                       "transaction_reference": "MP241110.0930.B77",
                                                       "paid_on": ""})
        claim = ManualMembershipPayment.objects.get()
        self.assertRedirects(response, claim_url(claim))
        status = self.client.get(claim_url(claim))
        self.assertContains(status, "Pending verification")
        self.assertContains(status, "MP241110.0930.B77")
        # The pay page now forwards to the open submission; no second form.
        self.assertRedirects(self.client.get(pay_url(pending)), claim_url(claim))

        mine = self.client.get(reverse("memberships:mine"))
        self.assertContains(mine, "being verified")
        self.assertNotContains(mine, "Cancel this choice")
        dashboard = self.client.get(reverse("dashboard"))
        self.assertContains(dashboard, "Your payment is being verified.")
        self.assertNotContains(dashboard, OBSOLETE_NOTICE)

    def test_form_errors_are_clear_and_nothing_is_saved(self):
        pending = self.choose()
        response = self.client.post(pay_url(pending), {"amount": "4000", "payer_phone": "07",
                                                       "transaction_reference": "x"})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Please correct the errors below.", status_code=400)
        self.assertContains(response, "valid phone number", status_code=400)
        self.assertFalse(ManualMembershipPayment.objects.exists())
        response = self.client.post(pay_url(pending), {"amount": "4000", "payer_phone": "0781234567",
                                                       "transaction_reference": "MP12345678"})
        self.assertContains(response, "The amount must be exactly TZS 5,000", status_code=400)

    def test_rejected_member_can_resubmit(self):
        claim = self.submit()
        manual.reject(self.verifier, claim, reason="REFERENCE_MISMATCH")
        page = self.client.get(claim_url(claim))
        self.assertContains(page, "The transaction reference does not match")
        self.assertContains(page, "Submit corrected details")
        self.assertContains(self.client.get(reverse("dashboard")), "Payment not confirmed")
        self.assertEqual(self.client.get(pay_url(claim.subscription)).status_code, 200)

    def test_approved_member_sees_active_membership(self):
        self.approve(self.submit())
        page = self.client.get(reverse("memberships:mine"))
        self.assertContains(page, "Monthly")
        self.assertContains(page, "Active")
        self.assertNotContains(page, "Complimentary")

    def test_member_can_withdraw_own_claim(self):
        claim = self.submit()
        response = self.client.post(claim_url(claim), {"action": "cancel"})
        self.assertRedirects(response, claim_url(claim))
        claim.refresh_from_db()
        self.assertEqual(claim.status, C.CANCELLED)

    def test_other_members_records_are_invisible(self):
        claim = self.submit()
        other = make_user("other@example.com")
        self.client.force_login(other)
        self.assertEqual(self.client.get(claim_url(claim)).status_code, 404)
        self.assertEqual(self.client.post(claim_url(claim), {"action": "cancel"}).status_code, 404)
        self.assertEqual(self.client.get(pay_url(claim.subscription)).status_code, 404)
        self.assertEqual(self.client.post(pay_url(claim.subscription), {}).status_code, 404)

    def test_members_cannot_reach_verification(self):
        claim = self.submit()
        for url in (reverse("administration:membership_payments"), review_url(claim),
                    reverse("administration:payment_instructions")):
            response = self.client.post(url, {"action": "approve", "verified_amount": "5000",
                                              "verified_reference": claim.transaction_reference,
                                              "receipt_confirmed": "on"})
            self.assertEqual(response.status_code, 302)
            self.assertIn(reverse("admin:login"), response["Location"])
        claim.refresh_from_db()
        self.assertEqual(claim.status, C.PENDING_VERIFICATION)
        self.assertFalse(JournalEntry.objects.exists())

    def test_unverified_member_must_confirm_email_first(self):
        unverified = make_user("unverified@example.com", verified=False)
        self.client.force_login(unverified)
        pending_url = reverse("payments:manual_pay", args=["00000000-0000-0000-0000-000000000001"])
        self.assertRedirects(self.client.get(pending_url), reverse("accounts:verification_required"),
                             fetch_redirect_response=False)

    def test_csrf_is_enforced(self):
        pending = self.choose()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.member)
        response = client.post(pay_url(pending), {"amount": "5000", "payer_phone": "0781234567",
                                                  "transaction_reference": "MP12345678"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ManualMembershipPayment.objects.exists())

    @override_settings(MANUAL_MEMBERSHIP_PAYMENTS_ENABLED=False)
    def test_pages_do_not_exist_when_switched_off(self):
        pending = self.choose()
        self.assertEqual(self.client.get(pay_url(pending)).status_code, 404)
        self.assertEqual(self.client.post(reverse("memberships:choose", args=["monthly"])).status_code, 302)
        mine = self.client.get(reverse("memberships:mine"))
        self.assertContains(mine, OBSOLETE_NOTICE)
        self.assertNotContains(mine, "Airtel")

    def test_unpublished_details_show_a_clear_notice(self):
        pending = self.choose()
        manual.retire_instruction(self.root)
        page = self.client.get(pay_url(pending))
        self.assertContains(page, "Payments are temporarily unavailable")
        self.assertNotContains(page, BUSINESS_NUMBER)
        self.assertNotContains(page, 'name="transaction_reference"')

    def test_financial_pages_do_not_claim_live_earnings(self):
        self.approve(self.submit())
        dashboard = self.client.get(reverse("dashboard"))
        self.assertContains(dashboard, "Real rewards are not available yet.")
        self.assertEqual(self.client.get(reverse("payments:history")).status_code, 404)  # Sandbox stays off.


class ConsoleTests(ManualPaymentTestCase):
    def test_queue_and_detail_for_verifiers(self):
        claim = self.submit(reference="MP31313131")
        self.client.force_login(self.verifier)
        queue = self.client.get(reverse("administration:membership_payments"))
        self.assertContains(queue, "member@example.com")
        self.assertContains(queue, "MP31313131")
        self.assertContains(queue, "TZS 5,000")
        self.assertContains(queue, "Pending verification")
        self.assertContains(queue, "official Airtel business records")
        self.assertContains(self.client.get(reverse("administration:overview")),
                            "Membership payments awaiting verification")
        detail = self.client.get(review_url(claim))
        self.assertContains(detail, "Approve payment and activate membership")
        self.assertContains(detail, 'name="receipt_confirmed"')
        self.assertContains(detail, "+255781234567")

    def test_approve_through_the_console(self):
        claim = self.submit(reference="MP31313131")
        self.client.force_login(self.verifier)
        response = self.client.post(review_url(claim), {"action": "approve", "verified_amount": "5000",
                                                        "verified_reference": "mp31313131"})
        self.assertEqual(response.status_code, 400)  # Receipt not confirmed.
        response = self.client.post(review_url(claim), {"action": "approve", "verified_amount": "4000",
                                                        "verified_reference": "MP31313131",
                                                        "receipt_confirmed": "on"})
        self.assertContains(response, "differs from the plan price", status_code=400)
        response = self.client.post(review_url(claim), {"action": "approve", "verified_amount": "5000",
                                                        "verified_reference": "MP31313131",
                                                        "receipt_confirmed": "on"})
        self.assertRedirects(response, review_url(claim))
        claim.refresh_from_db()
        self.assertEqual(claim.status, C.APPROVED)
        self.assertEqual(Subscription.objects.get(pk=claim.subscription_id).status, "ACTIVE")
        # A second click changes nothing.
        self.client.post(review_url(claim), {"action": "approve", "verified_amount": "5000",
                                             "verified_reference": "MP31313131", "receipt_confirmed": "on"})
        self.assertEqual(JournalEntry.objects.count(), 1)
        page = self.client.get(review_url(claim))
        self.assertContains(page, "Ledger entry")
        self.assertNotContains(page, "Approve payment and activate membership")

    def test_reject_through_the_console(self):
        claim = self.submit()
        self.client.force_login(self.verifier)
        response = self.client.post(review_url(claim), {"action": "reject", "reason": "OTHER", "note": ""})
        self.assertContains(response, "Explain the reason", status_code=400)
        response = self.client.post(review_url(claim), {"action": "reject", "reason": "NOT_FOUND"})
        self.assertRedirects(response, review_url(claim))
        claim.refresh_from_db()
        self.assertEqual(claim.status, C.REJECTED)

    def test_view_only_and_unprivileged_staff(self):
        claim = self.submit()
        viewer = staff_with("viewer@example.com", "view_manual_payments")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(reverse("administration:membership_payments")).status_code, 200)
        detail = self.client.get(review_url(claim))
        self.assertContains(detail, "You can view this payment but not approve or reject it.")
        response = self.client.post(review_url(claim), {"action": "approve", "verified_amount": "5000",
                                                        "verified_reference": claim.transaction_reference,
                                                        "receipt_confirmed": "on"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get(reverse("administration:payment_instructions")).status_code, 403)
        plain = staff_with("plain@example.com")
        self.client.force_login(plain)
        self.assertEqual(self.client.get(reverse("administration:membership_payments")).status_code, 403)
        self.assertEqual(self.client.get(review_url(claim)).status_code, 403)
        claim.refresh_from_db()
        self.assertEqual(claim.status, C.PENDING_VERIFICATION)

    def test_console_csrf_is_enforced(self):
        claim = self.submit()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.verifier)
        response = client.post(review_url(claim), {"action": "approve", "verified_amount": "5000",
                                                   "verified_reference": claim.transaction_reference,
                                                   "receipt_confirmed": "on"})
        self.assertEqual(response.status_code, 403)
        claim.refresh_from_db()
        self.assertEqual(claim.status, C.PENDING_VERIFICATION)

    def test_super_administrator_publishes_details(self):
        self.client.force_login(self.root)
        url = reverse("administration:payment_instructions")
        page = self.client.get(url)
        self.assertContains(page, BUSINESS_NUMBER)
        response = self.client.post(url, {"business_number": "12AB", "recipient_name": "X"})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(url, {"business_number": "765432", "recipient_name": "ClipCash Ltd",
                                          "instructions": "Use Lipa kwa Simu."})
        self.assertRedirects(response, url)
        self.assertEqual(manual.active_instruction().business_number, "765432")
        self.assertEqual(ManualPaymentInstruction.objects.count(), 2)
        self.client.post(url, {"action": "retire"})
        self.assertIsNone(manual.active_instruction())
        self.assertTrue(AuditEvent.objects.filter(action="membership.payment_instructions.retired").exists())

    @override_settings(MANUAL_MEMBERSHIP_PAYMENTS_ENABLED=False)
    def test_console_hidden_when_switched_off(self):
        self.client.force_login(self.root)
        self.assertEqual(self.client.get(reverse("administration:membership_payments")).status_code, 403)
        self.assertNotContains(self.client.get(reverse("administration:overview")), "Verification queue")


class LoginTests(ManualPaymentTestCase):
    def test_password_login_reaches_the_payment_flow(self):
        response = self.client.post(reverse("accounts:login"), {"username": "member@example.com",
                                                                "password": PASSWORD})
        self.assertEqual(response.status_code, 302)
        self.client.post(reverse("memberships:choose", args=["monthly"]))
        pending = Subscription.objects.get(member=self.member)
        self.assertEqual(self.client.get(pay_url(pending)).status_code, 200)
