from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone

from apps.accounts.tests.helpers import run_worker
from apps.audit.models import AuditEvent
from apps.core.jobs.services import enqueue
from apps.payments.models import SandboxPosting
from apps.payments.providers import PaymentsDisabled
from apps.wallets import wallet_services
from apps.wallets.models import JournalEntry
from apps.withdrawals import services
from apps.withdrawals.models import SandboxPayout, WithdrawalException, WithdrawalRequest

from .helpers import DemoWithdrawal, PayoutBeneficiary, WithdrawalsTestCase, make_user, token

D = DemoWithdrawal.Status


class BeneficiaryTests(WithdrawalsTestCase):
    def test_mobile_money_is_normalized_masked_and_minimized(self):
        beneficiary = self.mobile(number="0712 345 678")
        self.assertEqual((beneficiary.identifier_last4, beneficiary.network, beneficiary.verification),
                         ("5678", "MPESA", "UNVERIFIED"))
        self.assertRegex(beneficiary.identifier_fingerprint, r"^[0-9a-f]{64}$")
        stored = " ".join(str(getattr(beneficiary, f.attname)) for f in PayoutBeneficiary._meta.fields)
        self.assertNotIn("712345678", stored)  # The full number is never stored.
        self.assertEqual(str(beneficiary), "Mobile money •••• 5678")
        with self.assertRaises(ValidationError):  # Same number in another format = duplicate.
            self.mobile(number="+255712345678")

    def test_invalid_destinations(self):
        cases = [
            {"kind": "MOBILE_MONEY", "network": "MPESA", "identifier": "+254712345678"},
            {"kind": "MOBILE_MONEY", "network": "MPESA", "identifier": "07123"},
            {"kind": "MOBILE_MONEY", "network": "TELCO", "identifier": "0712345678"},
            {"kind": "BANK", "bank_code": "C", "identifier": "0150123456"},
            {"kind": "BANK", "bank_code": "CRDB", "identifier": "ABC123456"},
            {"kind": "BANK", "bank_code": "CRDB", "identifier": "123"},
            {"kind": "CARD", "identifier": "4111111111111111"},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValidationError):
                services.add_beneficiary(self.member, holder_name="Asha Juma", **case)
        with self.assertRaises(ValidationError):
            services.add_beneficiary(self.member, kind="BANK", bank_code="CRDB", identifier="0150123456",
                                     holder_name=" ")
        bank = services.add_beneficiary(self.member, kind="BANK", bank_code="crdb", identifier="0150-1234-5678",
                                        holder_name="Asha Juma")
        self.assertEqual((bank.bank_code, bank.identifier_last4), ("CRDB", "5678"))

    def test_limits_ownership_and_staff(self):
        for index in range(3):
            self.mobile(number=f"071234567{index}")
        with self.assertRaises(services.InvalidState):
            self.mobile(number="0712345679")
        with self.assertRaises(services.NotAuthorized):
            self.mobile(member=self.manager)
        other = make_user("other@x.example")
        beneficiary = PayoutBeneficiary.objects.filter(member=self.member).first()
        with self.assertRaises(services.NotFound):
            services.deactivate_beneficiary(other, beneficiary.pk)
        services.deactivate_beneficiary(self.member, beneficiary.pk)
        services.deactivate_beneficiary(self.member, beneficiary.pk)  # Idempotent.
        self.assertEqual(AuditEvent.objects.filter(action="withdrawals.beneficiary.deactivated").count(), 1)

    @override_settings(WITHDRAWALS_BENEFICIARY_CHANGES_PER_DAY=1)
    def test_rate_limited(self):
        self.mobile()
        with self.assertRaises(services.InvalidState):
            self.mobile(number="0754000111")

    def test_database_immutability_and_demo_check(self):
        beneficiary = self.mobile()
        for change in ({"holder_name": "Someone Else"}, {"identifier_last4": "0000"}, {"network": "AIRTEL"}):
            with self.subTest(change=change), self.assertRaises(DatabaseError), transaction.atomic():
                PayoutBeneficiary.objects.filter(pk=beneficiary.pk).update(**change)
        with self.assertRaises(DatabaseError), transaction.atomic():
            PayoutBeneficiary.objects.filter(pk=beneficiary.pk).delete()
        checked = services.demo_verify_beneficiary(self.member, beneficiary.pk)
        self.assertEqual(checked.verification, "DEMO_VERIFIED")
        with override_settings(DEBUG=False, PAYMENTS_SANDBOX_ALLOWED=False), self.assertRaises(PaymentsDisabled):
            services.demo_verify_beneficiary(self.member, beneficiary.pk)


class RealWithdrawalTests(WithdrawalsTestCase):
    def test_real_withdrawals_are_always_denied(self):
        decision = services.withdrawal_eligibility(self.member)
        self.assertFalse(decision)
        self.assertEqual(decision.reasons[:2], ["withdrawals_locked", "no_wallet"])
        wallet = wallet_services.ensure_wallet(self.member)
        beneficiary = services.demo_verify_beneficiary(self.member, self.mobile().pk)
        decision = services.withdrawal_eligibility(self.member, amount=10_000, beneficiary=beneficiary)
        self.assertEqual(decision.available, 0)
        self.assertIn("insufficient_balance", decision.reasons)
        self.assertIn("beneficiary_cooling_off", decision.reasons)
        wallet_services.place_restriction(self.root, wallet, kind="FRAUD_REVIEW", reason="Review needed")
        self.assertIn("wallet_restricted", services.withdrawal_eligibility(self.member).reasons)
        self.assertEqual(services.withdrawal_eligibility(self.member, amount=100).reasons[-1], "below_minimum")
        self.assertIn("not_eligible_account", services.withdrawal_eligibility(self.manager).reasons)
        with self.assertRaises(services.WithdrawalsLocked):
            services.request_withdrawal(self.member, amount=10_000, beneficiary=beneficiary)
        self.assertTrue(AuditEvent.objects.filter(action="withdrawals.request.denied").exists())
        with self.assertRaises(DatabaseError), transaction.atomic():
            WithdrawalRequest.objects.create(member=self.member, wallet=wallet, beneficiary=beneficiary,
                                             amount=10_000, idempotency_key=token())

    def test_eligibility_ignores_sandbox_money(self):
        wallet_services.ensure_wallet(self.member)
        self.settled_deposit(500_000)
        intent = self.membership_intent()
        from apps.payments import simulator

        simulator.simulate(self.root, intent, "pay")
        self.assertEqual(services.withdrawal_eligibility(self.member, amount=5_000).available, 0)


class DemoWorkflowTests(WithdrawalsTestCase):
    def test_standard_flow_with_separation_of_duties(self):
        before = (JournalEntry.objects.count(), SandboxPosting.objects.count())
        demo = self.demo(50_000)
        self.assertEqual((demo.status, demo.high_risk), (D.REQUESTED, True))  # New beneficiary flag.
        demo = services.start_review(self.approver, demo, expected_status=D.REQUESTED)
        with self.assertRaises(services.NotAuthorized):
            services.approve(self.root, demo, expected_status=D.UNDER_REVIEW)  # Creator.
        demo = services.approve(self.approver, demo, expected_status=D.UNDER_REVIEW)
        self.assertEqual((demo.status, demo.approved_by), (D.UNDER_REVIEW, self.approver))  # Needs a second.
        with self.assertRaises(services.NotAuthorized):
            services.approve(self.approver, demo, expected_status=D.UNDER_REVIEW)
        demo = services.approve(self.third, demo, expected_status=D.UNDER_REVIEW)
        self.assertEqual(demo.status, D.APPROVED)
        for executor in (self.approver, self.third):
            with self.assertRaises(services.NotAuthorized):
                services.initiate_payout(executor, demo)
        demo = services.initiate_payout(self.root, demo)
        self.assertEqual(demo.status, D.PAYOUT_PENDING)
        self.assertRegex(demo.payout_reference, r"^DPO-[0-9A-F]{16}$")
        self.assertEqual(services.simulate(self.root, demo, "succeed"), (200, "ACCEPTED"))
        self.assertEqual(services.simulate(self.root, demo, "replay"), (200, "DUPLICATE"))
        self.assertEqual(services.simulate(self.root, demo, "forged"), (401, "signature_invalid"))
        demo.refresh_from_db()
        self.assertEqual(demo.status, D.PAID)
        self.assertEqual([d.action for d in demo.decisions.all()],
                         ["created", "review_started", "approval_recorded", "approved", "payout_initiated",
                          "completed"])
        self.assertEqual((JournalEntry.objects.count(), SandboxPosting.objects.count()), before)  # No money.

    def test_standard_risk_needs_one_approver(self):
        beneficiary = self.mobile()
        PayoutBeneficiary.objects.filter(pk=beneficiary.pk).update(verification="DEMO_VERIFIED")
        with self.settings(WITHDRAWALS_BENEFICIARY_COOLING_HOURS=0):
            demo = self.demo(20_000, beneficiary=PayoutBeneficiary.objects.get(pk=beneficiary.pk))
        self.assertEqual((demo.high_risk, demo.flags), (False, []))
        demo = services.start_review(self.approver, demo, expected_status=D.REQUESTED)
        demo = services.approve(self.approver, demo, expected_status=D.UNDER_REVIEW)
        self.assertEqual(demo.status, D.APPROVED)

    def test_stale_illegal_and_duplicate(self):
        demo = self.demo()
        with self.assertRaises(services.StaleDecision):
            services.approve(self.approver, demo, expected_status=D.UNDER_REVIEW)
        with self.assertRaises(services.InvalidState):  # One open request per member.
            self.demo(beneficiary=demo.beneficiary)
        self.assertTrue(AuditEvent.objects.filter(action="withdrawals.demo.duplicate_refused").exists())
        key = token()
        services.cancel(self.root, demo)
        again = services.cancel(self.root, demo)  # Duplicate cancellation: no new decision.
        self.assertEqual(again.decisions.filter(action="cancelled").count(), 1)
        first = services.create_demo_withdrawal(self.root, self.member, demo.beneficiary, amount=10_000,
                                                idempotency_key=key)
        second = services.create_demo_withdrawal(self.root, self.member, demo.beneficiary, amount=10_000,
                                                 idempotency_key=key)
        self.assertEqual(first.pk, second.pk)
        for change in ({"simulated_amount": 1}, {"status": D.PAID}, {"destination_last4": "0000"}):
            with self.subTest(change=change), self.assertRaises((DatabaseError, IntegrityError)), \
                    transaction.atomic():
                DemoWithdrawal.objects.filter(pk=first.pk).update(**change)
        with self.assertRaises(DatabaseError), transaction.atomic():
            DemoWithdrawal.objects.filter(pk=first.pk).delete()

    def test_limits_and_permissions(self):
        beneficiary = self.mobile()
        with self.assertRaises(services.InvalidState):
            services.create_demo_withdrawal(self.root, self.member, beneficiary, amount=100, idempotency_key=token())
        with self.assertRaises(ValidationError):
            services.create_demo_withdrawal(self.root, self.member, beneficiary, amount=10.5, idempotency_key=token())
        for actor in (self.member, self.owner, self.manager):
            with self.subTest(actor=actor.email), self.assertRaises(services.NotAuthorized):
                services.create_demo_withdrawal(actor, self.member, beneficiary, amount=10_000,
                                                idempotency_key=token())

    def test_timeout_is_uncertain_and_reconciled_with_same_reference(self):
        demo = services.initiate_payout(self.root, self.approved())
        reference = demo.payout_reference
        services.simulate(self.root, demo, "timeout")
        demo.refresh_from_db()
        self.assertEqual(demo.status, D.UNCERTAIN)  # Not "failed".
        self.assertEqual(services.reconcile_payouts(), 1)
        demo.refresh_from_db()
        self.assertEqual((demo.status, demo.payout_reference), (D.UNCERTAIN, reference))
        SandboxPayout.objects.filter(reference=reference).update(unreachable=False, status="SUCCEEDED", sequence=9)
        services.reconcile_payouts()
        demo.refresh_from_db()
        self.assertEqual((demo.status, demo.payout_reference), (D.PAID, reference))
        self.assertEqual(SandboxPayout.objects.count(), 1)  # Never re-sent with a new reference.
        services.simulate(self.root, demo, "return")
        demo.refresh_from_db()
        self.assertEqual(demo.status, D.RETURNED)

    def test_failure_is_terminal(self):
        demo = services.initiate_payout(self.root, self.approved())
        services.simulate(self.root, demo, "fail")
        demo.refresh_from_db()
        self.assertEqual(demo.status, D.FAILED)
        with self.assertRaises(services.InvalidState):
            services.cancel(self.root, demo)
        services.simulate(self.root, demo, "succeed")  # A late "success" notice cannot reopen it.
        demo.refresh_from_db()
        self.assertEqual(demo.status, D.FAILED)

    def test_jobs(self):
        stale = self.demo()
        self.assertEqual(services.expire_demo(now=timezone.now() + timedelta(days=8)), 1)
        self.assertEqual(services.expire_demo(now=timezone.now() + timedelta(days=8)), 0)
        stale.refresh_from_db()
        self.assertEqual(stale.status, D.EXPIRED)
        demo = services.initiate_payout(self.root, self.approved())
        later = timezone.now() + timedelta(hours=2)
        self.assertEqual(services.detect_stuck(now=later), 1)
        services.detect_stuck(now=later)
        self.assertEqual(WithdrawalException.objects.filter(code="payout_stuck").count(), 1)
        enqueue("withdrawals.reconcile_payouts", {})
        enqueue("withdrawals.housekeeping", {})
        run_worker()
        demo.refresh_from_db()
        self.assertEqual(demo.status, D.PAYOUT_PENDING)
        exception = WithdrawalException.objects.get()
        services.resolve_exception(self.root, exception, note="Reconciled manually")

    def test_production_refuses_demo(self):
        beneficiary = self.mobile()
        with override_settings(DEBUG=False, PAYMENTS_SANDBOX_ALLOWED=False), self.assertRaises(PaymentsDisabled):
            services.create_demo_withdrawal(self.root, self.member, beneficiary, amount=10_000,
                                            idempotency_key=token())

    def test_audit_has_no_account_numbers(self):
        services.add_beneficiary(self.member, kind="BANK", bank_code="NMB", identifier="40110012345678",
                                 holder_name="Asha Juma")
        self.demo()
        for event in AuditEvent.objects.filter(category="WITHDRAWALS"):
            text = str(event.metadata) + event.target_repr
            self.assertNotIn("40110012345678", text)
            self.assertNotIn("712345678", text)

    def test_wallets_stay_zero(self):
        wallet = wallet_services.ensure_wallet(self.member)
        services.simulate(self.root, services.initiate_payout(self.root, self.approved()), "succeed")
        self.assertEqual(wallet_services.balances(wallet).available, 0)
        self.assertEqual(wallet_services.reconcile()["findings"], 0)

