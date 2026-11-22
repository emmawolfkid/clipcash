"""PAY-UX-01: "I've Paid — Request Verification" with optional evidence.

Members may request verification with no receipt, no reference and no paying phone. Nothing is
activated by the request; staff still find the payment in the provider records, and approval
records the reference they found as the claim's single-use key (database-enforced, migration 0009).
"""

import shutil
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.helpers import make_user
from apps.audit.models import AuditEvent
from apps.memberships.models import Subscription
from apps.payments import manual
from apps.payments.models import ManualMembershipPayment
from apps.wallets.models import JournalEntry

from .manual_helpers import BUSINESS_NUMBER, RECIPIENT, ManualPaymentTestCase, airtel_reference, staff_with

C = ManualMembershipPayment.Status
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
BUTTON = "I've Paid — Request Verification"


def pay_url(subscription):
    return reverse("payments:manual_pay", args=[subscription.pk])


class Pay01UxTestCase(ManualPaymentTestCase):
    def request(self, member=None, subscription=None, **details):
        subscription = subscription or self.choose(member)
        return manual.submit_claim(member or subscription.member, subscription, **details)

    def verify(self, claim, reference, actor=None):
        return manual.approve(actor or self.verifier, claim, verified_amount=claim.amount,
                              verified_reference=reference, receipt_confirmed=True, recipient_confirmed=True)


class RequestWithoutEvidenceTests(Pay01UxTestCase):
    def test_request_without_receipt_reference_or_phone(self):
        pending = self.choose()
        claim, created = self.request(subscription=pending)
        self.assertTrue(created)
        self.assertEqual((claim.status, claim.amount, claim.transaction_reference, claim.reference_key,
                          claim.payer_phone), (C.PENDING_VERIFICATION, 5_000, "", "", ""))
        self.assertFalse(hasattr(claim, "evidence") and claim.evidence)
        # A request is never proof: nothing is posted or activated.
        self.assertEqual(Subscription.objects.get(pk=pending.pk).status, Subscription.Status.PENDING)
        self.assertFalse(JournalEntry.objects.exists())
        event = AuditEvent.objects.get(action="membership.manual_payment.submitted")
        self.assertEqual(event.metadata["details"], [])
        from apps.notifications.models import Notification
        notice = Notification.objects.get(recipient=self.member, kind="membership.payment_submitted")
        self.assertIn("no transaction reference given", notice.message)
        given, _ = self.request(member=make_user("given@example.com"), transaction_reference="MP34343434")
        self.assertIn("reference MP34343434", Notification.objects.get(
            recipient=given.member, kind="membership.payment_submitted").message)

    def test_phone_is_optional_and_any_number_is_accepted(self):
        self.member.phone_number = "+255712345678"
        self.member.save(update_fields=["phone_number"])
        claim, _ = self.request(payer_phone="0781 234 567")  # Someone else's phone.
        self.assertEqual(claim.payer_phone, "+255781234567")
        self.assertNotEqual(claim.payer_phone, self.member.phone_number)
        other = make_user("no-phone@example.com")
        self.assertEqual(self.request(member=other, payer_phone="   ")[0].payer_phone, "")
        # A number that is typed must still be a valid number.
        with self.assertRaises(ValidationError) as caught:
            self.request(member=make_user("bad-phone@example.com"), payer_phone="12345")
        self.assertIn("payer_phone", caught.exception.message_dict)

    def test_a_given_reference_is_still_checked_and_single_use(self):
        claim, _ = self.request(transaction_reference="mp-2411.0930")
        self.assertEqual((claim.transaction_reference, claim.reference_key), ("MP-2411.0930", "MP24110930"))
        with self.assertRaises(ValidationError) as caught:
            self.request(member=make_user("second@example.com"), transaction_reference="MP24110930")
        self.assertIn("transaction_reference", caught.exception.message_dict)

    def test_amount_is_the_issued_price_and_cannot_be_changed(self):
        pending = self.choose()
        with self.assertRaises(ValidationError):
            self.request(subscription=pending, amount=4_000)
        self.assertEqual(self.request(subscription=pending)[0].amount, pending.price_amount)

    def test_receipt_is_optional_and_stored_privately_when_given(self):
        root = tempfile.mkdtemp(prefix="clipcash-payux-test-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with self.settings(PAYMENTS_EVIDENCE_ROOT=root):
            claim, _ = self.request(evidence=SimpleUploadedFile("r.png", PNG, content_type="image/png"))
            self.assertEqual(claim.evidence.content_type, "image/png")
            self.assertEqual(len([p for p in Path(root).rglob("*") if p.is_file()]), 1)
            with self.assertRaises(ValidationError):  # Still checked by content.
                self.request(member=make_user("fake@example.com"),
                             evidence=SimpleUploadedFile("r.png", b"<html>", content_type="image/png"))
        self.assertFalse(str(settings.PAYMENTS_EVIDENCE_ROOT).startswith(str(settings.STATIC_ROOT or "\0")))


class DuplicateRequestTests(Pay01UxTestCase):
    def test_repeated_clicks_return_the_same_request(self):
        pending = self.choose()
        first, created = self.request(subscription=pending)
        again, created_again = self.request(subscription=pending)
        self.assertEqual((first.pk, created, created_again), (again.pk, True, False))
        self.assertEqual(ManualMembershipPayment.objects.filter(subscription=pending).count(), 1)

    def test_a_different_second_request_is_refused_while_one_is_open(self):
        pending = self.choose()
        self.request(subscription=pending)
        with self.assertRaises(manual.InvalidState) as caught:
            self.request(subscription=pending, transaction_reference=airtel_reference())
        self.assertEqual(caught.exception.code, "already_submitted")
        self.assertEqual(ManualMembershipPayment.objects.filter(subscription=pending).count(), 1)

    def test_many_members_can_wait_without_references(self):
        claims = [self.request(member=make_user(f"wait{i}@example.com"))[0] for i in range(3)]
        self.assertEqual({c.status for c in claims}, {C.PENDING_VERIFICATION})

    def test_double_post_from_the_page_creates_one_request(self):
        self.client.force_login(self.member)
        pending = self.choose()
        for _ in range(3):
            self.client.post(pay_url(pending), {})
        claim = ManualMembershipPayment.objects.get(subscription=pending)
        self.assertEqual((claim.transaction_reference, claim.payer_phone), ("", ""))


class ReviewTests(Pay01UxTestCase):
    def test_unauthorized_people_cannot_approve(self):
        claim, _ = self.request()
        viewer = staff_with("viewer@example.com", "view_manual_payments")
        plain = make_user("staff@example.com", role=self.verifier.Role.CONTENT_MANAGER)
        for actor in (self.member, viewer, plain):
            with self.subTest(actor=actor.email), self.assertRaises(manual.NotAuthorized):
                self.verify(claim, "MP55555555", actor=actor)
        self.assertEqual(ManualMembershipPayment.objects.get(pk=claim.pk).status, C.PENDING_VERIFICATION)
        self.assertFalse(JournalEntry.objects.exists())
        for user, status in ((plain, 403), (self.member, 302)):
            self.client.force_login(user)
            response = self.client.post(reverse("administration:membership_payment", args=[claim.pk]), {
                "action": "approve", "verified_amount": "5000", "verified_reference": "MP55555555",
                "receipt_confirmed": "on", "recipient_confirmed": "on",
                "verified_receiving_number": BUSINESS_NUMBER, "verified_recipient_name": RECIPIENT})
            self.assertEqual(response.status_code, status)
        self.assertEqual(ManualMembershipPayment.objects.get(pk=claim.pk).status, C.PENDING_VERIFICATION)

    def test_staff_see_pending_requests_with_every_detail(self):
        self.member.phone_number = "+255712345678"
        self.member.save(update_fields=["phone_number"])
        bare, _ = self.request()
        detailed, _ = self.request(member=make_user("detailed@example.com"), payer_phone="0781234567",
                                   transaction_reference="MP77007700")
        self.client.force_login(self.verifier)
        queue = self.client.get(reverse("administration:membership_payments"))
        for claim in (bare, detailed):
            self.assertContains(queue, reverse("administration:membership_payment", args=[claim.pk]))
        self.assertContains(queue, "Not given")
        self.assertContains(queue, "MP77007700")
        page = self.client.get(reverse("administration:membership_payment", args=[bare.pk]))
        for text in ("Registered phone", "+255712345678", "Monthly", "TZS 5,000", "Not given by the member",
                     "The member gave no transaction reference", "Approve payment and activate membership",
                     "Reject payment", "None attached"):
            self.assertContains(page, text)
        self.assertNotContains(page, "The same transaction reference appears in other submissions")
        detail = self.client.get(reverse("administration:membership_payment", args=[detailed.pk]))
        self.assertContains(detail, "+255781234567")

    def test_approval_records_the_reviewers_reference_and_activates_once(self):
        pending = self.choose()
        claim, _ = self.request(subscription=pending)
        approved, changed = self.verify(claim, "mp.2024.ab12")
        self.assertTrue(changed)
        self.assertEqual((approved.status, approved.transaction_reference, approved.reference_key,
                          approved.verified_reference_key), (C.APPROVED, "MP.2024.AB12", "MP2024AB12", "MP2024AB12"))
        pending.refresh_from_db()
        self.assertEqual((pending.status, pending.payment_reference), ("ACTIVE", "AIRTEL-LIPA:MP2024AB12"))
        entry = JournalEntry.objects.get()
        self.assertEqual((entry.external_reference, entry.total_amount), ("MP2024AB12", 5_000))
        event = AuditEvent.objects.get(action="membership.manual_payment.approved")
        self.assertEqual(event.metadata["reference_source"], "reviewer")
        # Approving again changes nothing and posts nothing.
        again, changed_again = self.verify(ManualMembershipPayment.objects.get(pk=claim.pk), "MP2024AB12")
        self.assertFalse(changed_again)
        self.assertEqual(JournalEntry.objects.count(), 1)
        self.assertEqual(Subscription.objects.filter(member=self.member, status="ACTIVE").count(), 1)

    def test_reviewer_must_enter_a_reference(self):
        claim, _ = self.request()
        with self.assertRaises(ValidationError) as caught:
            self.verify(claim, "")
        self.assertIn("verified_reference", caught.exception.message_dict)
        self.assertEqual(ManualMembershipPayment.objects.get(pk=claim.pk).status, C.PENDING_VERIFICATION)

    def test_a_used_transaction_cannot_back_a_second_membership(self):
        first, _ = self.request(transaction_reference="MP31313131")
        self.verify(first, "MP31313131")
        other = make_user("other@example.com")
        claim, _ = self.request(member=other)
        with self.assertRaises(ValidationError) as caught:
            self.verify(claim, "mp 3131 3131")
        self.assertIn("verified_reference", caught.exception.message_dict)
        claim.refresh_from_db()
        self.assertEqual((claim.status, claim.reference_key), (C.PENDING_VERIFICATION, ""))
        self.assertEqual(JournalEntry.objects.count(), 1)
        refusal = AuditEvent.objects.filter(action="membership.manual_payment.refused").latest("occurred_at")
        self.assertEqual(refusal.metadata, {"action": "approve", "reason": "duplicate_reference"})
        # An open request that quotes the reference blocks it too (whose payment is unclear).
        third, _ = self.request(member=make_user("third@example.com"), transaction_reference="MP42424242")
        with self.assertRaises(ValidationError):
            self.verify(self.request(member=make_user("fourth@example.com"))[0], "MP42424242")
        self.assertEqual(ManualMembershipPayment.objects.get(pk=third.pk).status, C.PENDING_VERIFICATION)

    def test_rejection_does_not_activate(self):
        pending = self.choose()
        claim, _ = self.request(subscription=pending)
        rejected, changed = manual.reject(self.verifier, claim, reason="NOT_FOUND")
        self.assertTrue(changed)
        self.assertEqual((rejected.status, rejected.reference_key), (C.REJECTED, ""))
        self.assertEqual(Subscription.objects.get(pk=pending.pk).status, Subscription.Status.PENDING)
        self.assertFalse(JournalEntry.objects.exists())
        # The member may request verification again for the same choice.
        self.assertTrue(self.request(subscription=pending)[1])


class DatabaseGuardTests(Pay01UxTestCase):
    """The optional details never loosen the rules for approved claims (migration 0009)."""

    def assertRefused(self, func):
        with self.assertRaises(DatabaseError), transaction.atomic():
            func()

    def test_a_missing_reference_cannot_be_filled_in_outside_approval(self):
        claim, _ = self.request()
        rows = ManualMembershipPayment.objects.filter(pk=claim.pk)
        self.assertRefused(lambda: rows.update(transaction_reference="MP90909090", reference_key="MP90909090"))
        self.assertRefused(lambda: rows.update(payer_phone="+255781234567"))
        # Approval without the verification record and its ledger posting is refused, reference or not.
        self.assertRefused(lambda: rows.update(
            status="APPROVED", reviewed_by=self.verifier, reviewed_at=timezone.now(), receipt_confirmed=True,
            verified_amount=5_000, verified_reference_key="MP90909090", transaction_reference="MP90909090",
            reference_key="MP90909090"))
        self.assertRefused(lambda: rows.update(
            status="APPROVED", reviewed_by=self.verifier, reviewed_at=timezone.now(), receipt_confirmed=True,
            verified_amount=5_000, verified_reference_key=""))

    def test_approved_claims_keep_their_reference(self):
        claim, _ = self.request()
        self.verify(claim, "MP80808080")
        rows = ManualMembershipPayment.objects.filter(pk=claim.pk)
        self.assertRefused(lambda: rows.update(transaction_reference="", reference_key=""))
        self.assertRefused(lambda: rows.update(reference_key="MP80808081"))

    def test_reference_and_phone_formats_are_enforced(self):
        pending = self.choose()
        base = dict(member=self.member, subscription=pending, instruction=self.instruction, plan_name="Monthly",
                    amount=5_000)
        self.assertRefused(lambda: ManualMembershipPayment.objects.create(
            **base, payer_phone="", transaction_reference="MP12121212", reference_key=""))  # Pair broken.
        self.assertRefused(lambda: ManualMembershipPayment.objects.create(
            **base, payer_phone="0781234567", transaction_reference="", reference_key=""))  # Not E.164.
        self.assertRefused(lambda: ManualMembershipPayment.objects.create(
            **base, payer_phone="", transaction_reference="X", reference_key="X"))  # Bad key format.

    def test_the_single_use_rule_still_holds_in_the_database(self):
        self.request(transaction_reference="MP60606060")
        other = make_user("dup@example.com")
        pending = self.choose(other)
        self.assertRefused(lambda: ManualMembershipPayment.objects.create(
            member=other, subscription=pending, instruction=self.instruction, plan_name="Monthly", amount=5_000,
            payer_phone="", transaction_reference="MP60606060", reference_key="MP60606060"))


class CustomerPageTests(Pay01UxTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.member)

    def test_pay_page_is_simple_and_mobile_ready(self):
        pending = self.choose()
        page = self.client.get(pay_url(pending))
        self.assertContains(page, BUTTON)
        self.assertContains(page, "Payment details (optional)")
        self.assertContains(page, "None of these is required.")
        self.assertContains(page, "Awaiting payment")
        self.assertContains(page, BUSINESS_NUMBER)
        self.assertContains(page, RECIPIENT)
        self.assertNotContains(page, 'name="amount"')
        self.assertNotContains(page, "style=")
        self.assertContains(page, "css/pages/payments/manual.css")
        html = page.content.decode()
        for name in ("payer_phone", "transaction_reference", "evidence"):
            field = html[html.index(f'name="{name}"') - 300:html.index(f'name="{name}"') + 200]
            self.assertNotIn("required", field, name)
        css = (Path(settings.BASE_DIR) / "static/css/pages/payments/manual.css").read_text(encoding="utf-8")
        self.assertIn(".manual-payment__optional", css)
        self.assertIn("@media (max-width: 480px)", css)
        self.assertIn("min-height: var(--touch-target)", css)

    def test_member_sees_clear_states(self):
        pending = self.choose()
        response = self.client.post(pay_url(pending), {"payer_phone": "", "transaction_reference": ""})
        claim = ManualMembershipPayment.objects.get()
        self.assertRedirects(response, reverse("payments:manual_claim", args=[claim.pk]))
        status = self.client.get(reverse("payments:manual_claim", args=[claim.pk]))
        self.assertContains(status, "Verification requested")
        self.assertContains(status, "Not given")
        self.assertContains(reverse_get(self.client, "memberships:mine"), "Verification requested")
        self.verify(claim, "MP12312312")
        self.assertContains(self.client.get(reverse("payments:manual_claim", args=[claim.pk])), "Membership active")
        other = make_user("rejected@example.com")
        self.client.force_login(other)
        rejected, _ = self.request(member=other)
        manual.reject(self.verifier, rejected, reason="NOT_FOUND")
        page = self.client.get(reverse("payments:manual_claim", args=[rejected.pk]))
        self.assertContains(page, "Verification unsuccessful")
        self.assertNotContains(page, "reference )")


def reverse_get(client, name):
    return client.get(reverse(name))
