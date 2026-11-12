"""PAY-01: manual (Airtel Lipa) membership payments, service layer, on PostgreSQL."""

from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone

from apps.accounts.tests.helpers import make_user
from apps.audit.models import AuditEvent
from apps.core import features
from apps.memberships import services as memberships
from apps.memberships.access import check_access
from apps.memberships.models import MembershipPlan, Subscription
from apps.notifications.models import Notification
from apps.payments import manual
from apps.payments.models import (
    ManualMembershipPayment,
    ManualPaymentInstruction,
    MembershipActivationPreview,
    PaymentIntent,
    SandboxPosting,
)
from apps.wallets import services as ledger
from apps.wallets.models import JournalEntry, Wallet

from .manual_helpers import BUSINESS_NUMBER, MANUAL_ON, ManualPaymentTestCase, airtel_reference, staff_with

C = ManualMembershipPayment.Status


class GateTests(ManualPaymentTestCase):
    def test_enabled_only_with_explicit_configuration(self):
        self.assertTrue(manual.manual_payments_enabled())
        self.assertTrue(manual.manual_payments_ready())
        cases = ({"MANUAL_MEMBERSHIP_PAYMENTS_ENABLED": False},
                 {"RESTRICTED_DEMO_MODE": True},
                 {"PAYMENTS_MODE": "sandbox", "PAYMENTS_SANDBOX_ALLOWED": True})
        for overrides in cases:
            with self.subTest(**overrides), override_settings(**overrides):
                self.assertFalse(manual.manual_payments_enabled())
                with self.assertRaises(manual.Unavailable):
                    self.submit()

    def test_memberships_feature_off_disables_payments(self):
        from apps.memberships.tests.helpers import enable_memberships

        enable_memberships(False)
        self.assertFalse(manual.manual_payments_enabled())

    def test_without_published_details_nothing_can_be_submitted(self):
        pending = self.choose()
        self.assertTrue(manual.retire_instruction(self.root))
        self.assertFalse(manual.manual_payments_ready())
        with self.assertRaises(manual.Unavailable) as caught:
            self.submit(pending)
        self.assertEqual(caught.exception.code, "no_instructions")

    def test_real_money_features_stay_locked(self):
        for key in features.FINANCIAL_FEATURES:
            self.assertFalse(features.is_enabled(key), key)
        from apps.core.checks import check_financial_locks

        self.assertEqual(check_financial_locks(None), [])

    def test_demo_email_waiver_is_off_while_manual_payments_are_configured(self):
        from apps.accounts.access import demo_verification_optional

        with override_settings(RESTRICTED_DEMO_MODE=True, DEMO_EMAIL_VERIFICATION_OPTIONAL=True):
            self.assertFalse(demo_verification_optional())


@MANUAL_ON
class DeployCheckTests(ManualPaymentTestCase):
    def test_contradictory_configurations_fail_deploy_checks(self):
        from apps.payments.checks import check_manual_membership_payments

        self.assertEqual(check_manual_membership_payments(None), [])
        with override_settings(RESTRICTED_DEMO_MODE=True, PAYMENTS_MODE="sandbox"):
            self.assertEqual(sorted(m.id for m in check_manual_membership_payments(None)),
                             ["payments.E003", "payments.E004"])
        with override_settings(MANUAL_MEMBERSHIP_PAYMENTS_ENABLED=False, RESTRICTED_DEMO_MODE=True):
            self.assertEqual(check_manual_membership_payments(None), [])


class SubmissionTests(ManualPaymentTestCase):
    def test_submission_is_only_a_claim(self):
        pending = self.choose()
        claim = self.submit(pending, reference=" mp241109.1534.a12345 ", phone="0781 234 567")
        self.assertEqual(claim.status, C.PENDING_VERIFICATION)
        self.assertEqual((claim.transaction_reference, claim.reference_key), ("MP241109.1534.A12345",
                                                                              "MP2411091534A12345"))
        self.assertEqual((claim.payer_phone, claim.amount, claim.plan_name), ("+255781234567", 5_000, "Monthly"))
        self.assertEqual(claim.instruction, self.instruction)
        pending.refresh_from_db()
        self.assertEqual(pending.status, Subscription.Status.PENDING)
        self.assertFalse(check_access(self.member))
        self.assertFalse(JournalEntry.objects.exists())
        event = AuditEvent.objects.get(action="membership.manual_payment.submitted")
        self.assertEqual(event.metadata, {"reference": claim.reference, "amount": 5_000, "plan_code": "monthly",
                                          "resubmission": False})
        self.assertNotIn("781234567", str(AuditEvent.objects.values_list("metadata", flat=True)))
        notice = Notification.objects.get(recipient=self.member, kind="membership.payment_submitted")
        self.assertIn("confirms the payment", notice.message)

    @override_settings(MANUAL_PAYMENTS_CLAIMS_PER_HOUR=100)
    def test_amount_must_equal_the_plan_price(self):
        pending = self.choose()
        for amount in (4_999, 5_001, 50_000, "5,000.50", "-5000", "five"):
            with self.subTest(amount=amount), self.assertRaises(ValidationError) as caught:
                self.submit(pending, amount=amount)
            self.assertIn("amount", caught.exception.message_dict)
        self.assertFalse(ManualMembershipPayment.objects.exists())
        self.assertEqual(self.submit(pending, amount="TZS 5,000").amount, 5_000)

    def test_price_comes_from_the_administrator_configured_plan_snapshot(self):
        pending = self.choose()
        memberships.update_plan(self.root, self.plan, {"price_amount": 7_000})
        with self.assertRaises(ValidationError):
            self.submit(pending, amount=7_000)  # The member's quote stays 5,000.
        self.assertEqual(self.submit(pending, amount=5_000).amount, 5_000)

    @override_settings(MANUAL_PAYMENTS_CLAIMS_PER_HOUR=100)
    def test_malformed_references_are_refused(self):
        pending = self.choose()
        for reference in (" ", "ABC12", "A" * 41, "AAAAAAAA", "000000", "MP<script>", "MP 12'34;--", "-MP12345"):
            with self.subTest(reference=reference), self.assertRaises(ValidationError) as caught:
                self.submit(pending, reference=reference)
            self.assertIn("transaction_reference", caught.exception.message_dict)

    def test_invalid_phone_and_future_date_are_refused(self):
        pending = self.choose()
        with self.assertRaises(ValidationError) as caught:
            self.submit(pending, phone="12345")
        self.assertIn("payer_phone", caught.exception.message_dict)
        with self.assertRaises(ValidationError) as caught:
            self.submit(pending, paid_on=timezone.localdate() + timedelta(days=3))
        self.assertIn("paid_on", caught.exception.message_dict)
        self.assertEqual(self.submit(pending, paid_on=timezone.localdate()).paid_on, timezone.localdate())

    @override_settings(MANUAL_PAYMENTS_CLAIMS_PER_HOUR=100)
    def test_duplicate_reference_refused_across_members_and_formats(self):
        self.submit(reference="MP2411-0915-AB12")
        other = make_user("other@example.com")
        for variant in ("mp24110915ab12", "MP 2411 0915 AB12", "MP2411.0915.AB12"):
            with self.subTest(variant=variant), self.assertRaises(ValidationError) as caught:
                self.submit(member=other, reference=variant)
            self.assertIn("transaction_reference", caught.exception.message_dict)
        self.assertEqual(ManualMembershipPayment.objects.count(), 1)
        refused = AuditEvent.objects.filter(action="membership.manual_payment.refused",
                                            metadata__reason="duplicate_reference")
        self.assertEqual(refused.count(), 3)

    def test_repeated_identical_submission_returns_the_open_claim(self):
        pending = self.choose()
        claim, created = manual.submit_claim(self.member, pending, amount=5_000, payer_phone="0781234567",
                                             transaction_reference="MP99887766")
        again, created_again = manual.submit_claim(self.member, pending, amount="5000", payer_phone="0781234567",
                                                   transaction_reference="mp-9988-7766")
        self.assertEqual((again.pk, created, created_again), (claim.pk, True, False))
        with self.assertRaises(manual.InvalidState) as caught:
            self.submit(pending, reference="MP11223344")
        self.assertEqual(caught.exception.code, "already_submitted")
        self.assertEqual(ManualMembershipPayment.objects.count(), 1)

    def test_members_pay_only_for_their_own_open_choice(self):
        pending = self.choose()
        other = make_user("other@example.com")
        with self.assertRaises(manual.NotAuthorized):
            self.submit(pending, member=other)
        unverified = make_user("unverified@example.com", verified=False)
        with self.assertRaises(manual.NotAuthorized):
            manual.submit_claim(unverified, pending, amount=5_000, payer_phone="0781234567",
                                transaction_reference=airtel_reference())
        later = pending.pending_until + timedelta(minutes=1)
        with mock.patch("apps.payments.manual.timezone.now", return_value=later),                 self.assertRaises(manual.InvalidState) as caught:
            self.submit(pending)
        self.assertEqual(caught.exception.code, "choice_expired")

    def test_withdrawn_plan_cannot_be_paid_for(self):
        pending = self.choose()
        memberships.set_plan_status(self.root, self.plan, MembershipPlan.Status.INACTIVE)
        with self.assertRaises(manual.InvalidState) as caught:
            self.submit(pending)
        self.assertEqual(caught.exception.code, "plan_unavailable")

    def test_submissions_are_rate_limited(self):
        pending = self.choose()
        for _ in range(5):
            with self.assertRaises(ValidationError):
                self.submit(pending, reference="bad")
        with self.assertRaises(manual.RateLimited):
            self.submit(pending)

    def test_member_can_withdraw_only_their_own_pending_claim(self):
        claim = self.submit()
        with self.assertRaises(manual.NotAuthorized):
            manual.cancel_claim(make_user("other@example.com"), claim)
        manual.cancel_claim(self.member, claim)
        claim.refresh_from_db()
        self.assertEqual(claim.status, C.CANCELLED)
        with self.assertRaises(manual.InvalidState):
            manual.cancel_claim(self.member, claim)
        # The reference is free again and the choice can be paid.
        self.assertEqual(self.submit(claim.subscription, reference=claim.transaction_reference).status,
                         C.PENDING_VERIFICATION)


class ApprovalTests(ManualPaymentTestCase):
    def test_approval_posts_to_the_real_ledger_and_activates_once(self):
        claim = self.submit()
        approved, changed = self.approve(claim)
        self.assertTrue(changed)
        self.assertEqual(approved.status, C.APPROVED)
        self.assertEqual((approved.reviewed_by, approved.verified_amount, approved.receipt_confirmed),
                         (self.verifier, 5_000, True))
        subscription = Subscription.objects.get(pk=claim.subscription_id)
        self.assertEqual(subscription.status, Subscription.Status.ACTIVE)
        self.assertEqual((subscription.activation_source, subscription.amount_paid, subscription.payment_reference),
                         ("PAYMENT", 5_000, "AIRTEL-LIPA:" + claim.reference_key))
        self.assertEqual(subscription.activated_by_id, self.verifier.pk)
        self.assertTrue(check_access(self.member))

        entry = JournalEntry.objects.get()
        self.assertEqual((entry.pk, entry.posting_type, entry.total_amount, entry.external_reference,
                          entry.source_type, entry.source_id),
                         (approved.ledger_entry_id, "membership.manual_payment_received", 5_000,
                          claim.reference_key, "manual_membership_payment", str(claim.pk)))
        lines = {(line.account.account_type, line.side, line.amount) for line in entry.lines.all()}
        self.assertEqual(lines, {("CASH", "DEBIT", 5_000), ("MEMBERSHIP_DEFERRED_REVENUE", "CREDIT", 5_000)})
        self.assertEqual(ledger.get_platform_account("CASH").balance, 5_000)
        self.assertEqual(ledger.get_platform_account("MEMBERSHIP_DEFERRED_REVENUE").balance, 5_000)

        # Sandbox, rewards, wallets and withdrawals are untouched.
        self.assertFalse(PaymentIntent.objects.exists())
        self.assertFalse(SandboxPosting.objects.exists())
        self.assertFalse(MembershipActivationPreview.objects.exists())
        self.assertFalse(Wallet.objects.filter(member=self.member).exists())
        self.assertFalse(JournalEntry.objects.exclude(posting_type="membership.manual_payment_received").exists())

        actions = set(AuditEvent.objects.values_list("action", flat=True))
        self.assertTrue({"membership.manual_payment.submitted", "membership.manual_payment.approved",
                         "membership.subscription.activated", "ledger.entry.posted"} <= actions)
        kinds = set(Notification.objects.filter(recipient=self.member).values_list("kind", flat=True))
        self.assertTrue({"membership.payment_submitted", "membership.payment_approved",
                         "membership.activated"} <= kinds)

    def test_repeated_approval_changes_nothing(self):
        claim = self.submit()
        self.approve(claim)
        again, changed = self.approve(claim)
        self.assertFalse(changed)
        self.assertEqual(again.status, C.APPROVED)
        self.assertEqual(JournalEntry.objects.count(), 1)
        self.assertEqual(Subscription.objects.filter(member=self.member, status="ACTIVE").count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="membership.manual_payment.approved").count(), 1)

    def test_reviewer_must_confirm_amount_reference_and_receipt(self):
        claim = self.submit(reference="MP55554444")
        cases = ({"confirmed": False}, {"amount": 4_000}, {"amount": 50_000}, {"reference": "MP55554445"},
                 {"reference": "bad"})
        for case in cases:
            with self.subTest(**case), self.assertRaises(ValidationError):
                self.approve(claim, **case)
        claim.refresh_from_db()
        self.assertEqual(claim.status, C.PENDING_VERIFICATION)
        self.assertFalse(JournalEntry.objects.exists())
        self.assertEqual(Subscription.objects.get(pk=claim.subscription_id).status, "PENDING")
        # The reference may be retyped in another format.
        self.assertTrue(self.approve(claim, reference="mp 5555 4444")[1])

    def test_unauthorized_people_cannot_approve_or_reject(self):
        claim = self.submit()
        viewer = staff_with("viewer@example.com", "view_manual_payments")
        plain_staff = staff_with("staff@example.com")
        for actor in (self.member, make_user("other@example.com"), viewer, plain_staff, None):
            with self.subTest(actor=actor):
                with self.assertRaises(manual.NotAuthorized):
                    self.approve(claim, actor=actor) if actor else manual.approve(
                        None, claim, verified_amount=5_000, verified_reference=claim.transaction_reference,
                        receipt_confirmed=True, recipient_confirmed=True)
                with self.assertRaises(manual.NotAuthorized):
                    manual.reject(actor, claim, reason="NOT_FOUND")
        self.assertTrue(manual.can_view(viewer))
        self.assertFalse(manual.can_view(plain_staff))
        claim.refresh_from_db()
        self.assertEqual(claim.status, C.PENDING_VERIFICATION)
        self.assertGreaterEqual(AuditEvent.objects.filter(action="membership.manual_payment.refused",
                                                          metadata__reason="not_authorized").count(), 8)

    def test_super_administrator_may_verify(self):
        claim = self.submit()
        self.assertTrue(self.approve(claim, actor=self.root)[1])

    def test_one_airtel_transaction_activates_one_membership(self):
        claim = self.submit(reference="MP10101010")
        self.approve(claim)
        other = make_user("other@example.com")
        with self.assertRaises(ValidationError):
            self.submit(member=other, reference="MP10101010")
        # Even if a row slipped past the service, the database refuses a second approval.
        with self.assertRaises(IntegrityError), transaction.atomic():
            ManualMembershipPayment.objects.create(
                member=other, subscription=self.choose(other), instruction=self.instruction, plan_name="Monthly",
                amount=5_000, payer_phone="+255781234567", transaction_reference="MP10101010",
                reference_key="MP10101010")

    def test_verification_after_the_choice_window_honours_the_quote(self):
        claim = self.submit()
        later = claim.subscription.pending_until + timedelta(hours=1)
        self.assertEqual(memberships.process_expiry(now=later)["cancelled"], 0)  # Kept open for the reviewer.
        memberships.set_plan_status(self.root, self.plan, MembershipPlan.Status.INACTIVE)
        with mock.patch("apps.payments.manual.timezone.now", return_value=later):
            self.assertTrue(self.approve(claim)[1])
        subscription = Subscription.objects.get(pk=claim.subscription_id)
        self.assertEqual((subscription.status, subscription.starts_at), ("ACTIVE", later))
        # Without an open claim the same stale choice is cancelled as before.
        other = make_user("other@example.com")
        memberships.set_plan_status(self.root, self.plan, MembershipPlan.Status.ACTIVE)
        stale = self.choose(other)
        self.assertEqual(memberships.process_expiry(now=stale.pending_until + timedelta(hours=1))["cancelled"], 1)

    def test_open_claim_blocks_replacing_or_cancelling_the_choice(self):
        claim = self.submit()
        annual = self.make_plan("annual", price=50_000, days=365)
        with self.assertRaises(memberships.NotEligible) as caught:
            memberships.choose_plan(self.member, annual)
        self.assertEqual(caught.exception.code, "payment_under_review")
        with self.assertRaises(memberships.InvalidTransition):
            memberships.cancel_pending(self.member, claim.subscription)
        with self.assertRaises(memberships.InvalidTransition):
            memberships.cancel(self.root, claim.subscription, Subscription.Reason.ADMIN_REVIEW)
        self.assertEqual(Subscription.objects.get(pk=claim.subscription_id).status, "PENDING")

    def test_renewal_is_queued_after_the_current_period(self):
        self.approve(self.submit())
        current = Subscription.objects.get(member=self.member, status="ACTIVE")
        near_end = current.expires_at - timedelta(days=3)  # Inside the renewal window.
        with mock.patch("apps.memberships.services.timezone.now", return_value=near_end),                 mock.patch("apps.payments.manual.timezone.now", return_value=near_end):
            renewal = self.submit(self.choose())
            self.approve(renewal)
        renewed = Subscription.objects.get(pk=renewal.subscription_id)
        self.assertEqual(renewed.starts_at, Subscription.objects.get(pk=current.pk).expires_at)
        self.assertEqual(JournalEntry.objects.count(), 2)

    def test_ledger_gate_follows_the_feature(self):
        claim = self.submit()
        with override_settings(MANUAL_MEMBERSHIP_PAYMENTS_ENABLED=False):
            with self.assertRaises(manual.Unavailable):
                self.approve(claim)
            from apps.wallets.exceptions import PostingDisabledError

            with self.assertRaises(PostingDisabledError):
                ledger.post_entry("membership.manual_payment_received",
                                  lines=[ledger.debit(ledger.get_platform_account("CASH"), 1),
                                         ledger.credit(ledger.get_platform_account("MEMBERSHIP_DEFERRED_REVENUE"), 1)],
                                  idempotency_key="x-test", source_type="test", description="x",
                                  external_reference="X1")
            with self.assertRaises(memberships.PaymentVerificationError):
                memberships.activate_with_manual_payment(claim.subscription, reference_key=claim.reference_key,
                                                         amount=5_000, actor=self.verifier,
                                                         submitted_at=claim.created_at, now=timezone.now())


class RejectionTests(ManualPaymentTestCase):
    def test_rejection_keeps_history_and_allows_resubmission(self):
        claim = self.submit(reference="MP77776666")
        rejected, changed = manual.reject(self.verifier, claim, reason="NOT_FOUND", note="Not in statement yet.")
        self.assertTrue(changed)
        self.assertEqual((rejected.status, rejected.rejection_reason, rejected.reviewed_by),
                         (C.REJECTED, "NOT_FOUND", self.verifier))
        self.assertEqual(Subscription.objects.get(pk=claim.subscription_id).status, "PENDING")
        notice = Notification.objects.get(recipient=self.member, kind="membership.payment_rejected")
        self.assertIn("no matching transaction", notice.message)
        self.assertIn("Not in statement yet.", notice.message)

        again = self.submit(claim.subscription, reference="MP77776666")  # Same reference after rejection.
        self.assertNotEqual(again.pk, claim.pk)
        self.assertTrue(AuditEvent.objects.get(action="membership.manual_payment.submitted",
                                               target_id=str(again.pk)).metadata["resubmission"])
        self.approve(again)
        self.assertEqual(list(ManualMembershipPayment.objects.filter(subscription=claim.subscription_id)
                              .order_by("created_at").values_list("status", flat=True)), [C.REJECTED, C.APPROVED])

    def test_rejection_rules(self):
        claim = self.submit()
        with self.assertRaises(ValidationError):
            manual.reject(self.verifier, claim, reason="MADE_UP")
        with self.assertRaises(ValidationError):
            manual.reject(self.verifier, claim, reason="OTHER", note="no")
        manual.reject(self.verifier, claim, reason="AMOUNT_MISMATCH")
        self.assertFalse(manual.reject(self.verifier, claim, reason="AMOUNT_MISMATCH")[1])  # Idempotent.
        with self.assertRaises(manual.InvalidState):
            self.approve(claim)

    def test_approved_payment_cannot_be_rejected(self):
        claim = self.submit()
        self.approve(claim)
        with self.assertRaises(manual.InvalidState):
            manual.reject(self.verifier, claim, reason="NOT_FOUND")


class InstructionTests(ManualPaymentTestCase):
    def test_only_super_administrators_publish_details_and_history_is_kept(self):
        with self.assertRaises(manual.NotAuthorized):
            manual.publish_instruction(self.verifier, business_number="999999", recipient_name="X Y")
        with self.assertRaises(ValidationError):
            manual.publish_instruction(self.root, business_number="12-AB", recipient_name="ClipCash")
        new = manual.publish_instruction(self.root, business_number=" 654 321 ", recipient_name="  ClipCash  Ltd ")
        self.assertEqual((new.business_number, new.recipient_name, new.is_active), ("654321", "ClipCash Ltd", True))
        old = ManualPaymentInstruction.objects.get(pk=self.instruction.pk)
        self.assertEqual((old.is_active, old.retired_by), (False, self.root))
        self.assertEqual(manual.active_instruction(), new)
        event = AuditEvent.objects.filter(action="membership.payment_instructions.published").latest("occurred_at")
        self.assertEqual(event.metadata["business_number"], "654321")
        self.assertTrue(event.metadata["replaced"])

    def test_claims_keep_the_details_the_member_was_shown(self):
        claim = self.submit()
        manual.publish_instruction(self.root, business_number="654321", recipient_name="New Name")
        claim.refresh_from_db()
        self.assertEqual(claim.instruction.business_number, BUSINESS_NUMBER)
        self.assertTrue(self.approve(claim)[1])  # Still verifiable after the details changed.


class DatabaseGuardTests(ManualPaymentTestCase):
    def assertRefused(self, func):
        with self.assertRaises(DatabaseError), transaction.atomic():
            func()

    def test_claims_cannot_be_deleted_or_rewritten(self):
        claim = self.submit()
        rows = ManualMembershipPayment.objects.filter(pk=claim.pk)
        self.assertRefused(rows.delete)
        self.assertRefused(lambda: rows.update(amount=1))
        self.assertRefused(lambda: rows.update(reference_key="MPFORGED999"))
        self.assertRefused(lambda: rows.update(member=make_user("other@example.com")))
        # Approval without the recorded verification and ledger entry is refused.
        self.assertRefused(lambda: rows.update(status="APPROVED"))
        self.assertRefused(lambda: rows.update(status="APPROVED", reviewed_by=self.verifier,
                                               reviewed_at=timezone.now(), receipt_confirmed=True,
                                               verified_amount=1, verified_reference_key=claim.reference_key))
        self.assertRefused(lambda: rows.update(status="REJECTED"))
        # Nobody approves their own payment, even directly.
        self.assertRefused(lambda: rows.update(status="REJECTED", reviewed_by=self.member,
                                               reviewed_at=timezone.now(), rejection_reason="OTHER"))

    def test_decided_claims_are_final(self):
        claim = self.submit()
        self.approve(claim)
        rows = ManualMembershipPayment.objects.filter(pk=claim.pk)
        self.assertRefused(lambda: rows.update(status="PENDING_VERIFICATION"))
        self.assertRefused(lambda: rows.update(review_note="changed"))

    def test_claims_are_created_pending(self):
        pending = self.choose()
        self.assertRefused(lambda: ManualMembershipPayment.objects.create(
            member=self.member, subscription=pending, instruction=self.instruction, plan_name="Monthly",
            amount=5_000, payer_phone="+255781234567", transaction_reference="MP12121212",
            reference_key="MP12121212", status="APPROVED"))

    def test_instructions_are_append_only(self):
        rows = ManualPaymentInstruction.objects.filter(pk=self.instruction.pk)
        self.assertRefused(lambda: rows.update(business_number="999999"))
        self.assertRefused(rows.delete)
        self.assertRefused(lambda: ManualPaymentInstruction.objects.create(
            business_number="777777", recipient_name="Second", is_active=True, created_by=self.root))


@override_settings(MANUAL_MEMBERSHIP_PAYMENTS_ENABLED=False)
class DisabledByDefaultTests(ManualPaymentTestCase):
    def test_nothing_is_possible_when_switched_off(self):
        self.assertFalse(features.is_enabled("manual_membership_payments"))
        self.assertFalse(manual.manual_payments_enabled())
        pending = self.choose()
        with self.assertRaises(manual.Unavailable):
            self.submit(pending)
        self.assertFalse(ManualMembershipPayment.objects.exists())


@MANUAL_ON
class ComplimentarySeparationTests(ManualPaymentTestCase):
    def test_no_complimentary_grants_outside_the_restricted_demo(self):
        with override_settings(MEMBERSHIPS_COMPLIMENTARY_GRANTS_ENABLED=True):
            self.assertFalse(memberships.complimentary_grants_available())
            with self.assertRaises(memberships.NotAuthorized):
                memberships.grant_complimentary(self.root, self.member, self.plan,
                                                expires_at=timezone.now() + timedelta(days=5), reason="Demo access")
        self.assertFalse(Subscription.objects.filter(activation_source="COMPLIMENTARY").exists())
