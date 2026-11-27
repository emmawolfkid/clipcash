"""FIN-02: real withdrawals paid manually, end to end on the real ledger (test database only)."""

from datetime import datetime, timedelta, timezone as dt_timezone

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import translation

from apps.core import features
from apps.earnings import services as earnings
from apps.earnings.models import SessionReward
from apps.notifications import inbox
from apps.notifications.models import Notification
from apps.wallets import services as ledger
from apps.wallets.models import JournalEntry, Wallet, WalletTransaction
from apps.withdrawals import protect, real, services
from apps.withdrawals.models import PayoutBeneficiary, WithdrawalRequest, WithdrawalSettings

from .fin02_helpers import PayoutTestCase, token

W = WithdrawalRequest.Status


class ActivationTests(PayoutTestCase):
    def test_off_by_default_and_settlement_only_mode(self):
        self.earn()
        self.beneficiary = self.destination()
        with override_settings(WITHDRAWALS_ENABLED=False, WITHDRAWALS_APPROVED=False):
            self.assertFalse(features.is_enabled("withdrawals"))
            self.assertIn("withdrawals_locked", services.withdrawal_eligibility(self.member, amount=200).reasons)
            with self.assertRaises(real.WithdrawalFlowError):
                self.submit()
        withdrawal = self.submit()
        real.approve(self.approver2, withdrawal, expected_status=W.REQUESTED)
        real.claim(self.operator, withdrawal)
        real.record_payment(self.operator, withdrawal, payout_reference="MPSETTLE01", evidence="Sent before pausing")
        with override_settings(WITHDRAWALS_ENABLED=False):  # New activity off; existing obligation settles.
            self.assertFalse(features.is_enabled("withdrawals"))
            self.assertTrue(features.is_enabled("withdrawal_settlement"))
            with self.assertRaises(real.WithdrawalFlowError):
                self.submit()
            self.assertEqual(real.verify_payment(self.payout_verifier, withdrawal, confirmed=True).status, W.PAID)

    def test_demo_mode_and_deploy_checks(self):
        from apps.core.checks import check_financial_locks

        with override_settings(RESTRICTED_DEMO_MODE=True):
            self.assertFalse(features.is_enabled("withdrawals"))
            self.assertFalse(features.is_enabled("withdrawal_settlement"))
            self.assertIn("core.E012", {m.id for m in check_financial_locks(None)})
        self.assertIn("core.W013", {m.id for m in check_financial_locks(None)})
        with override_settings(PAYOUT_DATA_KEYS=[]):
            self.assertIn("core.E014", {m.id for m in check_financial_locks(None)})
        self.assertFalse(features.is_enabled("live_payments"))


class DestinationTests(PayoutTestCase):
    def test_every_method_is_stored_encrypted_and_masked(self):
        cases = [("AIRTEL", "0688123456"), ("MPESA", "0754123456"), ("MIXX", "0714123456"),
                 ("HALOPESA", "0621123456"), ("BANK", "0150123456789")]
        for network, number in cases:
            beneficiary = self.destination(network=network, number=number)
            self.assertTrue(beneficiary.payable)
            self.assertNotIn(number[-6:], beneficiary.identifier_encrypted)
            plain = protect.decrypt(beneficiary.identifier_encrypted)
            self.assertTrue(plain.endswith(number[-6:]), plain)
            self.assertIn(f"•••• {number[-4:]}", beneficiary.destination_label)
            self.assertEqual(real.method_for(beneficiary), "BANK" if network == "BANK" else network)
            if network != "BANK":
                services.deactivate_beneficiary(self.member, beneficiary.pk)  # At most three active.
        bank = PayoutBeneficiary.objects.get(kind="BANK")
        self.assertEqual((bank.bank_code, bank.bank_name), ("CRDB", "CRDB Bank"))
        self.assertTrue(protect.decrypt(PayoutBeneficiary.objects.get(network="MPESA").identifier_encrypted)
                        .startswith("+255"))  # Normalized Tanzanian number.
        with self.assertRaises(ValidationError):
            services.add_beneficiary(self.member, kind="MOBILE_MONEY", network="MPESA", identifier="12345",
                                     holder_name="Asha")

    def test_destination_details_are_immutable_and_legacy_rows_are_not_payable(self):
        beneficiary = self.destination()
        with self.assertRaises((IntegrityError, DatabaseError)), transaction.atomic():
            PayoutBeneficiary.objects.filter(pk=beneficiary.pk).update(identifier_encrypted="x")
        with override_settings(PAYOUT_DATA_KEYS=[]):
            legacy = services.add_beneficiary(self.member, kind="MOBILE_MONEY", network="AIRTEL",
                                              identifier="0688999888", holder_name="Asha Juma")
        self.assertFalse(legacy.payable)
        self.earn()
        self.assertIn("no_payable_destination",
                      services.withdrawal_eligibility(self.member, amount=200, beneficiary=legacy).reasons)

    def test_adding_a_destination_on_the_web_needs_the_password(self):
        from apps.accounts.tests.helpers import PASSWORD

        self.client.force_login(self.member)
        url = reverse("withdrawals:destinations")
        data = {"action": "add", "kind": "MOBILE_MONEY", "network": "AIRTEL", "identifier": "0688123456",
                "holder_name": "Asha Juma"}
        self.client.post(url, data)
        self.assertFalse(PayoutBeneficiary.objects.exists())
        self.client.post(url, {**data, "current_password": PASSWORD})
        self.assertEqual(PayoutBeneficiary.objects.count(), 1)


class FeeTests(PayoutTestCase):
    def test_zero_fixed_and_percentage_fees_with_rounding(self):
        cfg = real.current_settings()
        self.assertEqual(real.quote(1000, cfg), real.Quote(1000, 0, "DEDUCTED", 1000, 1000))
        cfg = self.configure(fee_mode="FIXED", fee_fixed=300, fee_bearer="DEDUCTED")
        self.assertEqual(real.quote(1000, cfg), real.Quote(1000, 300, "DEDUCTED", 1000, 700))
        cfg = self.configure(fee_mode="FIXED", fee_fixed=300, fee_bearer="ADDED")
        self.assertEqual(real.quote(1000, cfg), real.Quote(1000, 300, "ADDED", 1300, 1000))
        cfg = self.configure(fee_mode="PERCENT", fee_percent_bps=150, fee_bearer="DEDUCTED")
        self.assertEqual(real.quote(333, cfg).fee, 5)  # 4.995 rounds UP to 5.
        self.assertEqual(real.quote(1000, cfg).fee, 15)
        self.assertEqual(real.quote(1, cfg).fee, 1)

    def test_fee_posting_and_settings_versions_are_preserved(self):
        self.earn(amount=1000)
        self.beneficiary = self.destination()
        self.configure(fee_mode="PERCENT", fee_percent_bps=200, fee_bearer="DEDUCTED")
        withdrawal = self.submit(amount=500)
        self.assertEqual((withdrawal.fee, withdrawal.wallet_deduction, withdrawal.net_payout), (10, 500, 490))
        version = withdrawal.settings_version.version
        self.configure(fee_mode="FIXED", fee_fixed=100)  # Later change never alters the request.
        withdrawal.refresh_from_db()
        self.assertEqual((withdrawal.fee, withdrawal.settings_version.version), (10, version))
        revenue = ledger.get_platform_account("OTHER_OPERATING_REVENUE")
        cash_before = ledger.get_platform_account("CASH").debit_balance
        self.pay(withdrawal)
        revenue.refresh_from_db()
        self.assertEqual(-revenue.debit_balance, 10)
        self.assertEqual(cash_before - ledger.get_platform_account("CASH").debit_balance, 490)
        with self.assertRaises((IntegrityError, DatabaseError)), transaction.atomic():
            WithdrawalSettings.objects.filter(version=version).update(min_amount=1)


class EligibilityTests(PayoutTestCase):
    def setUp(self):
        super().setUp()
        self.reward = self.earn(amount=500)
        self.beneficiary = self.destination()

    def reasons(self, amount, beneficiary=None):
        return services.withdrawal_eligibility(self.member, amount=amount,
                                               beneficiary=beneficiary or self.beneficiary).reasons

    def test_only_the_available_balance_can_be_withdrawn(self):
        self.assertEqual(self.reasons(500), [])
        self.assertIn("insufficient_balance", self.reasons(501))
        pending = self.earn(amount=300)
        pending_reward = SessionReward.objects.filter(status="AVAILABLE").count()
        self.assertEqual(pending_reward, 2)
        campaign = self.running_campaign(name="Still pending", demo_reward_per_view=400)
        self.fund(campaign=campaign, amount=1000)
        self.tzs_policy(campaign=campaign, amount=400)
        self.verified_view(campaign=campaign)  # 400 pending, not yet cleared.
        self.assertEqual((self.balances().available, self.balances().pending), (800, 400))
        self.assertIn("insufficient_balance", self.reasons(1000))
        self.assertTrue(pending)

    def test_limits_methods_identity_and_cooling_off(self):
        self.configure(min_amount=100, max_amount=400)
        self.assertIn("below_minimum", self.reasons(99))
        self.assertIn("above_maximum", self.reasons(401))
        self.configure(daily_request_limit=1, daily_amount_limit=300)
        self.assertIn("daily_limit", self.reasons(301))
        self.submit(amount=100)
        self.assertIn("frequency_limit", self.reasons(100))
        self.configure(methods=["AIRTEL", "BANK"])
        self.assertIn("method_disabled", self.reasons(100))
        self.configure(identity_required_from=50)
        self.assertIn("identity_required", self.reasons(100))
        fresh = self.destination(network="AIRTEL", number="0688777666", age_hours=0)
        self.assertIn("beneficiary_cooling_off", self.reasons(100, fresh))

    def test_daily_limits_follow_the_dar_es_salaam_day(self):
        self.configure(daily_request_limit=1)
        self.submit(amount=100)
        self.assertIn("frequency_limit", self.reasons(100))
        _start, end = earnings.day_bounds(earnings.local_day(self.clock.now))
        self.clock.now = end + timedelta(minutes=1)
        self.assertNotIn("frequency_limit", self.reasons(100))
        self.assertEqual(earnings.local_day(datetime(2026, 11, 25, 21, 1, tzinfo=dt_timezone.utc)).isoformat(),
                         "2026-11-26")


class WorkflowTests(PayoutTestCase):
    def setUp(self):
        super().setUp()
        self.earn(amount=1000)
        self.beneficiary = self.destination()

    def test_complete_manual_payout(self):
        withdrawal = self.submit(amount=600)
        self.assertEqual((self.balances().available, self.balances().reserved), (400, 600))
        self.assertEqual(withdrawal.status, W.REQUESTED)
        real.start_review(self.approver2, withdrawal, expected_status=W.REQUESTED)
        real.approve(self.approver2, withdrawal, expected_status=W.UNDER_REVIEW)
        real.claim(self.operator, withdrawal)
        number = real.reveal_destination(self.operator, withdrawal)
        self.assertEqual(number, "+255754123456")
        with self.assertRaises(real.NotAuthorized):
            real.reveal_destination(self.payout_verifier, withdrawal)
        real.record_payment(self.operator, withdrawal, payout_reference="MP26ABC123", evidence="ClipCash till 5544")
        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.status, W.VERIFYING)
        self.assertEqual(self.balances().reserved, 600)  # Not paid until verified.
        real.verify_payment(self.payout_verifier, withdrawal, confirmed=True)
        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.status, W.PAID)
        self.assertEqual((self.balances().available, self.balances().reserved), (400, 0))
        self.assertEqual(withdrawal.payout_entry.posting_type, "withdrawal.paid_manual")
        self.assertEqual(real.reconcile(), [])
        self.assertEqual(earnings.check_consistency(), [])
        from apps.wallets import wallet_services

        self.assertEqual(wallet_services.reconcile()["findings"], 0)
        kinds = set(Notification.objects.filter(recipient=self.member).values_list("kind", flat=True))
        self.assertTrue({"withdrawals.real_requested", "withdrawals.real_approved", "withdrawals.real_paid"} <= kinds)
        paid = Notification.objects.get(recipient=self.member, kind="withdrawals.real_paid")
        self.assertNotIn("754123456", paid.message)  # Never the full number.
        english = paid.title
        with translation.override("sw"):
            self.assertNotEqual(inbox.localize([paid])[0].title, english)
        self.assertEqual(list(withdrawal.events.values_list("to_status", flat=True)),
                         ["REQUESTED", "UNDER_REVIEW", "APPROVED", "PAYOUT_PENDING", "VERIFYING", "PAID"])

    def test_separation_of_duties_and_unauthorized_actions(self):
        withdrawal = self.submit(amount=300)
        with self.assertRaises(real.NotAuthorized):
            real.approve(self.operator, withdrawal, expected_status=W.REQUESTED)  # No approval permission.
        both = self.approver2
        both.user_permissions.add(*self.operator.user_permissions.all())
        both = type(both).objects.get(pk=both.pk)
        real.approve(both, withdrawal, expected_status=W.REQUESTED)
        with self.assertRaises(real.NotAuthorized):
            real.claim(both, withdrawal)  # The approver never sends the money.
        real.claim(self.operator, withdrawal)
        real.record_payment(self.operator, withdrawal, payout_reference="MPSEP0001", evidence="Sent by operator")
        verifier_too = self.operator
        verifier_too.user_permissions.add(*self.payout_verifier.user_permissions.all())
        verifier_too = type(verifier_too).objects.get(pk=verifier_too.pk)
        with self.assertRaises(real.NotAuthorized):
            real.verify_payment(verifier_too, withdrawal, confirmed=True)  # The operator never verifies.
        with self.assertRaises((IntegrityError, DatabaseError)), transaction.atomic():
            WithdrawalRequest.objects.filter(pk=withdrawal.pk).update(verified_by=self.operator)

    def test_duplicate_submission_and_duplicate_payment_reference(self):
        tok = token()
        first = self.submit(amount=200, tok=tok)
        again = self.submit(amount=200, tok=tok)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(self.balances().reserved, 200)
        second = self.submit(amount=200)
        self.pay(first, reference="MPDUP00001")
        real.approve(self.approver2, second, expected_status=W.REQUESTED)
        real.claim(self.operator, second)
        with self.assertRaises(ValidationError):
            real.record_payment(self.operator, second, payout_reference="mp-dup-00001", evidence="Same transaction")

    def test_cancel_and_reject_release_exactly_once(self):
        cancelled = self.submit(amount=300)
        real.cancel(self.member, cancelled)
        real.cancel(self.member, cancelled)  # Idempotent.
        rejected = self.submit(amount=400)
        real.reject(self.approver2, rejected, expected_status=W.REQUESTED, reason_code="destination_mismatch")
        with self.assertRaises(real.WithdrawalFlowError):
            real.reject(self.approver2, rejected, expected_status=W.REJECTED, reason_code="other")
        self.assertEqual(JournalEntry.objects.filter(posting_type="withdrawal.released").count(), 2)
        self.assertEqual((self.balances().available, self.balances().reserved), (1000, 0))
        claimed = self.submit(amount=100)
        real.approve(self.approver2, claimed, expected_status=W.REQUESTED)
        real.claim(self.operator, claimed)
        with self.assertRaises(real.WithdrawalFlowError):
            real.cancel(self.member, claimed)  # Money may be on its way: no cancellation.

    def test_failed_and_uncertain_payouts(self):
        uncertain = self.submit(amount=300)
        real.approve(self.approver2, uncertain, expected_status=W.REQUESTED)
        real.claim(self.operator, uncertain)
        real.report_problem(self.operator, uncertain, note="Provider timed out; outcome unknown")
        uncertain.refresh_from_db()
        self.assertEqual(uncertain.status, W.RECONCILIATION)
        self.assertEqual(self.balances().reserved, 300)  # Never refunded automatically.
        self.assertIsNone(uncertain.release_entry)
        with self.assertRaises(real.WithdrawalFlowError):
            real.claim(self.operator, uncertain, expected_status=W.APPROVED)  # No blind retry.
        with self.assertRaises(real.NotAuthorized):
            real.confirm_failed(self.operator, uncertain, note="Operator cannot confirm own failure")
        real.confirm_failed(self.payout_verifier, uncertain, note="Statement shows nothing was sent")
        uncertain.refresh_from_db()
        self.assertEqual((uncertain.status, self.balances().reserved, self.balances().available), (W.FAILED, 0, 1000))
        found = self.submit(amount=200)
        real.approve(self.approver2, found, expected_status=W.REQUESTED)
        real.claim(self.operator, found)
        real.record_payment(self.operator, found, payout_reference="MPFOUND01", evidence="Receipt arrived late")
        real.report_problem(self.payout_verifier, found, note="Provider record delayed")
        real.verify_payment(self.payout_verifier, found, confirmed=True)
        found.refresh_from_db()
        self.assertEqual(found.status, W.PAID)

    def test_destination_change_after_submission_keeps_the_snapshot(self):
        withdrawal = self.submit(amount=200)
        services.deactivate_beneficiary(self.member, self.beneficiary.pk)
        self.destination(network="AIRTEL", number="0688000111")
        withdrawal.refresh_from_db()
        self.assertEqual((withdrawal.destination_last4, withdrawal.method), ("3456", "MPESA"))
        with self.assertRaises((IntegrityError, DatabaseError)), transaction.atomic():
            WithdrawalRequest.objects.filter(pk=withdrawal.pk).update(destination_last4="0111")
        self.pay(withdrawal)

    def test_wallet_restriction_and_reward_reversal_with_reserved_funds(self):
        withdrawal = self.submit(amount=1000)  # Everything available is now reserved.
        reward = SessionReward.objects.get(status="AVAILABLE")
        earnings.staff_reverse(self.rewards_admin, reward, reason="Fraud found after clearance")
        reward.refresh_from_db()
        self.assertEqual(reward.status, "RECOVERY_REQUIRED")  # Reserved money is never taken.
        self.assertEqual(Wallet.objects.get(member=self.member).status, Wallet.Status.RESTRICTED)
        with self.assertRaises(real.WithdrawalFlowError):
            real.approve(self.approver2, withdrawal, expected_status=W.REQUESTED)
        real.reject(self.approver2, withdrawal, expected_status=W.REQUESTED, reason_code="suspected_fraud")
        earnings.reconcile()  # Retries the reversal now that the money is available again.
        reward.refresh_from_db()
        self.assertEqual(reward.status, "REVERSED")
        self.assertEqual((self.balances().available, self.balances().reserved), (0, 0))

    def test_identity_check_flow(self):
        self.configure(identity_required_from=0)
        self.assertIn("identity_required", services.withdrawal_eligibility(self.member, amount=200).reasons)
        identity = real.submit_identity(self.member, legal_name="Asha Juma Mwinyi", id_type="NIDA",
                                        id_number="19900101-12345-00001-23")
        self.assertNotIn("12345", identity.id_encrypted)
        self.assertEqual(identity.id_last4, "0123")
        with self.assertRaises(real.NotAuthorized):
            real.review_identity(self.approver2, identity, approve=True)
        real.review_identity(self.id_checker, identity, approve=True)
        self.assertNotIn("identity_required", services.withdrawal_eligibility(self.member, amount=200).reasons)
        with self.assertRaises(real.WithdrawalFlowError):
            real.submit_identity(self.member, legal_name="Someone Else", id_type="NIDA", id_number="ABCDEF123")

    def test_database_guards(self):
        withdrawal = self.submit(amount=200)
        for change in ({"status": "PAID"}, {"amount": 1}, {"net_payout": 1}):
            with self.subTest(change=change), self.assertRaises((IntegrityError, DatabaseError)), transaction.atomic():
                WithdrawalRequest.objects.filter(pk=withdrawal.pk).update(**change)
        with self.assertRaises((IntegrityError, DatabaseError)), transaction.atomic():
            WithdrawalRequest.objects.filter(pk=withdrawal.pk).delete()
        wallet = Wallet.objects.get(member=self.member)
        from apps.wallets.exceptions import LedgerError

        with self.assertRaises((IntegrityError, DatabaseError, LedgerError)), transaction.atomic():
            ledger.post_entry("reward.pending", lines=[ledger.debit(wallet.reserved_account, 10),
                                                       ledger.credit(wallet.available_account, 10)],
                              idempotency_key="bad-reserved", source_type="test", description="not allowed")
        with self.assertRaises((IntegrityError, DatabaseError)), transaction.atomic():
            WalletTransaction.objects.filter(wallet=wallet).update(amount=1)

    def test_console_pages_and_payout_actions_on_the_web(self):
        withdrawal = self.submit(amount=250)
        for user, names in ((self.root, ("withdrawal_requests", "withdrawal_settings", "withdrawal_identities")),):
            self.client.force_login(user)
            for name in names:
                self.assertEqual(self.client.get(reverse(f"administration:{name}")).status_code, 200, name)
        url = reverse("administration:withdrawal_request", args=[withdrawal.pk])
        self.client.force_login(self.approver2)
        self.client.post(url, {"action": "approve", "expected_status": "REQUESTED"})
        self.client.force_login(self.operator)
        self.client.post(url, {"action": "claim", "expected_status": "APPROVED"})
        page = self.client.post(url, {"action": "reveal"}).content.decode()
        self.assertIn("+255754123456", page)
        self.client.post(url, {"action": "record", "payout_reference": "MPWEB0001", "evidence": "Sent from till"})
        self.client.force_login(self.payout_verifier)
        self.client.post(url, {"action": "verify", "confirmed": "on"})
        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.status, W.PAID)
        self.client.force_login(self.member)
        self.assertNotEqual(self.client.get(reverse("administration:withdrawal_requests")).status_code, 200)
        member_page = self.client.get(reverse("withdrawals:request", args=[withdrawal.pk])).content.decode()
        self.assertIn("MPWEB0001", member_page)

    def test_member_review_and_confirm_on_the_web(self):
        self.client.force_login(self.member)
        page = self.client.get(reverse("withdrawals:overview"), {"destination": self.beneficiary.pk, "amount": "300"})
        self.assertContains(page, "You will receive")
        tok = page.context["review"]["token"]
        response = self.client.post(reverse("withdrawals:request_new"), {"destination": self.beneficiary.pk,
                                                                        "amount": "300", "token": tok})
        withdrawal = WithdrawalRequest.objects.get()
        self.assertRedirects(response, reverse("withdrawals:request", args=[withdrawal.pk]))
        self.client.post(reverse("withdrawals:request_new"), {"destination": self.beneficiary.pk, "amount": "300",
                                                              "token": tok})
        self.assertEqual(WithdrawalRequest.objects.count(), 1)  # Double submission.


class CreditsTests(PayoutTestCase):
    def test_credits_and_pending_rewards_are_never_withdrawable(self):
        from apps.rewards import services as rewards

        campaign = self.running_campaign(name="Credits only")
        rewards.approve_policy(self.approver, rewards.create_policy(self.root, campaign, reward_amount=50))
        self.verified_view(campaign=campaign)
        rewards.evaluate_pending()
        self.beneficiary = self.destination()
        decision = services.withdrawal_eligibility(self.member, amount=50, beneficiary=self.beneficiary)
        self.assertTrue({"insufficient_balance", "no_wallet"} & set(decision.reasons), decision.reasons)
        self.assertEqual(decision.available, 0)
        with self.assertRaises(real.WithdrawalFlowError):
            self.submit(amount=50)

