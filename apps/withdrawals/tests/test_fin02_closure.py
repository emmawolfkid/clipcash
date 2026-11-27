"""FIN-02 final closure: restricted wallets and already-sent payments, encryption and key rotation,
leak prevention, identity rules, staff separation and permission assignment (test database only)."""

import json
import logging

from cryptography.fernet import Fernet
from django.contrib.auth.models import Permission
from django.test import override_settings
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.earnings import services as earnings
from apps.earnings.models import SessionReward
from apps.notifications.models import Notification
from apps.wallets import wallet_services
from apps.wallets.models import JournalEntry, Wallet, WalletTransaction
from apps.withdrawals import protect, real, services
from apps.withdrawals.models import WithdrawalEvent, WithdrawalRequest

from .fin02_helpers import PayoutTestCase, staff

W = WithdrawalRequest.Status
NUMBER = "0754123456"


class RestrictedWalletTests(PayoutTestCase):
    def setUp(self):
        super().setUp()
        self.earn(amount=1000)
        self.beneficiary = self.destination(number=NUMBER)
        self.wallet = Wallet.objects.get(member=self.member)

    def restrict(self):
        wallet_services.place_restriction(self.root, self.wallet, kind="FRAUD_REVIEW", reason="Closure test review")

    def approved(self, amount=300):
        withdrawal = self.submit(amount=amount)
        real.approve(self.approver2, withdrawal, expected_status=W.REQUESTED)
        return withdrawal

    def test_restriction_before_approval_blocks_and_rejection_releases(self):
        withdrawal = self.submit(amount=300)
        self.restrict()
        with self.assertRaises(real.WithdrawalFlowError):
            real.approve(self.approver2, withdrawal, expected_status=W.REQUESTED)
        real.reject(self.approver2, withdrawal, expected_status=W.REQUESTED, reason_code="suspected_fraud")
        self.assertEqual((self.balances().available, self.balances().reserved), (1000, 0))

    def test_restriction_before_claim_blocks_the_claim(self):
        withdrawal = self.approved()
        self.restrict()
        with self.assertRaises(real.WithdrawalFlowError):
            real.claim(self.operator, withdrawal)
        real.reject(self.approver2, withdrawal, expected_status=W.APPROVED, reason_code="suspected_fraud")
        self.assertEqual(self.balances().reserved, 0)

    def test_restriction_after_claim_before_sending_can_be_closed_as_failed(self):
        withdrawal = self.approved()
        real.claim(self.operator, withdrawal)
        self.restrict()  # The operator must not send; a verifier confirms nothing left ClipCash.
        real.confirm_failed(self.payout_verifier, withdrawal, note="Not sent: wallet restricted before sending")
        withdrawal.refresh_from_db()
        self.assertEqual((withdrawal.status, self.balances().reserved, self.balances().available),
                         (W.FAILED, 0, 1000))

    def test_restriction_after_the_payment_was_sent_still_settles(self):
        withdrawal = self.approved()
        real.claim(self.operator, withdrawal)
        real.record_payment(self.operator, withdrawal, payout_reference="MPSENT0001", evidence="Sent before review")
        self.restrict()
        real.verify_payment(self.payout_verifier, withdrawal, confirmed=True)
        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.status, W.PAID)
        self.assertEqual((self.balances().available, self.balances().reserved), (700, 0))
        self.assertIn("Wallet restricted", WithdrawalEvent.objects.get(withdrawal=withdrawal, to_status=W.PAID).note)
        self.assertEqual(real.reconcile(), [])
        self.assertEqual(earnings.check_consistency(), [])

    def test_restriction_during_reconciliation_payment_found_or_failed(self):
        found = self.approved(amount=300)
        real.claim(self.operator, found)
        real.record_payment(self.operator, found, payout_reference="MPRECON001", evidence="Provider delayed")
        real.report_problem(self.payout_verifier, found, note="No confirmation yet")
        self.restrict()
        real.verify_payment(self.payout_verifier, found, confirmed=True)
        found.refresh_from_db()
        self.assertEqual(found.status, W.PAID)
        with self.assertRaises(real.WithdrawalFlowError):
            self.submit(amount=200)  # No new request while the wallet is restricted.

    def test_fraud_reversal_while_reserved_then_sent_payment_settles(self):
        withdrawal = self.approved(amount=1000)
        real.claim(self.operator, withdrawal)
        real.record_payment(self.operator, withdrawal, payout_reference="MPFRAUD001", evidence="Sent before fraud found")
        reward = SessionReward.objects.get(status="AVAILABLE")
        earnings.staff_reverse(self.rewards_admin, reward, reason="Fraud found while money was reserved")
        reward.refresh_from_db()
        self.assertEqual(reward.status, "RECOVERY_REQUIRED")
        self.assertEqual(self.wallet.__class__.objects.get(pk=self.wallet.pk).status, Wallet.Status.RESTRICTED)
        real.verify_payment(self.payout_verifier, withdrawal, confirmed=True)  # The money really left.
        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.status, W.PAID)
        earnings.reconcile()
        reward.refresh_from_db()
        self.assertEqual(reward.status, "RECOVERY_REQUIRED")  # Recovered manually; nothing went negative.
        self.assertEqual((self.balances().available, self.balances().reserved), (0, 0))
        self.assertEqual(earnings.check_consistency(), [])

    def test_no_duplicate_payment_or_release(self):
        withdrawal = self.approved()
        real.claim(self.operator, withdrawal)
        real.record_payment(self.operator, withdrawal, payout_reference="MPONCE0001", evidence="Sent once")
        real.verify_payment(self.payout_verifier, withdrawal, confirmed=True)
        real.verify_payment(self.payout_verifier, withdrawal, confirmed=True)  # Replay: unchanged.
        with self.assertRaises(real.WithdrawalFlowError):
            real.confirm_failed(self.payout_verifier, withdrawal, note="Too late, already paid")
        self.assertEqual(JournalEntry.objects.filter(posting_type="withdrawal.paid_manual").count(), 1)
        self.assertEqual(JournalEntry.objects.filter(posting_type="withdrawal.released").count(), 0)
        failed = self.approved(amount=100)
        real.claim(self.operator, failed)
        real.confirm_failed(self.payout_verifier, failed, note="Transfer bounced")
        with self.assertRaises(real.WithdrawalFlowError):
            real.confirm_failed(self.payout_verifier, failed, note="Twice")
        self.assertEqual(JournalEntry.objects.filter(posting_type="withdrawal.released").count(), 1)


class EncryptionTests(PayoutTestCase):
    def test_encrypt_decrypt_and_rotation_with_disposable_keys(self):
        old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        with override_settings(PAYOUT_DATA_KEYS=[old]):
            token = protect.encrypt("+255754123456")
            self.assertEqual(protect.decrypt(token), "+255754123456")
            self.assertNotIn("754123456", token)
        with override_settings(PAYOUT_DATA_KEYS=[new, old]):  # Rotation: newest first, old kept.
            self.assertEqual(protect.decrypt(token), "+255754123456")
            fresh = protect.encrypt("+255754999999")
        with override_settings(PAYOUT_DATA_KEYS=[new]):
            self.assertEqual(protect.decrypt(fresh), "+255754999999")
            with self.assertRaises(protect.ProtectionUnavailable):
                protect.decrypt(token)  # Retiring the old key too early loses access: keep it listed.
        with override_settings(PAYOUT_DATA_KEYS=[]):
            self.assertFalse(protect.available())
            with self.assertRaises(protect.ProtectionUnavailable):
                protect.encrypt("x")
            with self.assertRaises(protect.ProtectionUnavailable):
                protect.decrypt(token)
        with override_settings(PAYOUT_DATA_KEYS=["not-a-valid-key"]), self.assertRaises(Exception):
            protect.encrypt("x")

    def test_missing_key_at_reveal_is_reported_not_crashed(self):
        self.earn(amount=500)
        self.beneficiary = self.destination(number=NUMBER)
        withdrawal = self.submit(amount=200)
        real.approve(self.approver2, withdrawal, expected_status=W.REQUESTED)
        real.claim(self.operator, withdrawal)
        self.client.force_login(self.operator)
        url = reverse("administration:withdrawal_request", args=[withdrawal.pk])
        with override_settings(PAYOUT_DATA_KEYS=[Fernet.generate_key().decode()]):  # Wrong key.
            response = self.client.post(url, {"action": "reveal"}, follow=True)
        self.assertContains(response, "payout data key is not configured")
        self.assertNotContains(response, "+255754123456")

    def test_no_plaintext_numbers_in_logs_audit_notifications_or_ledger(self):
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage())
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        self.earn(amount=800)
        self.beneficiary = self.destination(number=NUMBER)
        real.submit_identity(self.member, legal_name="Asha Juma Mwinyi", id_type="NIDA", id_number="NIDA77665544")
        withdrawal = self.submit(amount=300)
        self.pay(withdrawal)
        secrets = ("754123456", "NIDA77665544", "77665544")
        haystacks = {
            "logs": " ".join(records),
            "audit": json.dumps(list(AuditEvent.objects.values_list("metadata", flat=True)), default=str),
            "notifications": " ".join(t + " " + m for t, m in Notification.objects.values_list("title", "message")),
            "ledger": " ".join(JournalEntry.objects.values_list("description", flat=True)),
            "wallet": " ".join(WalletTransaction.objects.values_list("reference", flat=True)),
            "events": " ".join(WithdrawalEvent.objects.values_list("note", flat=True)),
        }
        for name, text in haystacks.items():
            for secret in secrets:
                self.assertNotIn(secret, text, name)
        self.assertEqual(withdrawal.destination_last4, "3456")


class IdentityRuleTests(PayoutTestCase):
    def setUp(self):
        super().setUp()
        self.earn(amount=1000)
        self.beneficiary = self.destination(number=NUMBER)

    def test_membership_and_viewing_never_need_identity(self):
        self.assertIsNone(real.identity_of(self.member))
        self.assertTrue(SessionReward.objects.filter(member=self.member).exists())  # Watched and earned.
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("viewing:list")).status_code, 200)

    def test_required_identity_cannot_be_bypassed_by_a_later_settings_change(self):
        self.configure(identity_required_from=0)
        with self.assertRaises(real.WithdrawalFlowError):
            self.submit(amount=200)  # Not verified: refused at submission.
        identity = real.submit_identity(self.member, legal_name="Asha Juma Mwinyi", id_type="PASSPORT",
                                        id_number="TZ1234567")
        real.review_identity(self.id_checker, identity, approve=True)
        withdrawal = self.submit(amount=200)
        self.assertTrue(withdrawal.identity_verified)
        self.configure(identity_required_from=None)  # Relaxing the rule later changes nothing for requests.
        self.assertTrue(real.identity_required(withdrawal.amount, withdrawal.settings_version))

    def test_approval_rechecks_identity_under_the_request_rules(self):
        self.configure(identity_required_from=500)
        withdrawal = self.submit(amount=200)  # Below the threshold: no identity needed.
        self.configure(identity_required_from=0)
        real.approve(self.approver2, withdrawal, expected_status=W.REQUESTED)  # Rules of its own version.
        identity = real.submit_identity(self.member, legal_name="Asha Juma Mwinyi", id_type="NIDA",
                                        id_number="NIDA11223344")
        with self.assertRaises(real.NotAuthorized):
            real.review_identity(self.approver2, identity, approve=True)  # Needs the identity permission.

    def test_identity_number_is_encrypted_and_only_last_four_shown(self):
        identity = real.submit_identity(self.member, legal_name="Asha Juma Mwinyi", id_type="NIDA",
                                        id_number="NIDA99887766")
        self.assertNotIn("99887766", identity.id_encrypted)
        self.assertEqual(protect.decrypt(identity.id_encrypted), "NIDA99887766")
        self.client.force_login(self.id_checker)
        page = self.client.get(reverse("administration:withdrawal_identities")).content.decode()
        self.assertIn("7766", page)
        self.assertNotIn("99887766", page)


class StaffSeparationTests(PayoutTestCase):
    def test_two_distinct_staff_are_the_minimum(self):
        self.earn(amount=500)
        self.beneficiary = self.destination(number=NUMBER)
        withdrawal = self.submit(amount=300)
        reviewer = staff("a@x.example", "withdrawals.approve_withdrawals", "withdrawals.verify_payouts")
        operator = staff("b@x.example", "withdrawals.process_payouts")
        real.approve(reviewer, withdrawal, expected_status=W.REQUESTED)
        both = staff("c@x.example", "withdrawals.approve_withdrawals", "withdrawals.process_payouts",
                     "withdrawals.verify_payouts")
        real.claim(operator, withdrawal)
        real.record_payment(operator, withdrawal, payout_reference="MPTWO00001", evidence="Sent by B")
        with self.assertRaises(real.NotAuthorized):
            real.verify_payment(operator, withdrawal, confirmed=True)  # B never verifies its own payment.
        real.verify_payment(reviewer, withdrawal, confirmed=True)  # A approved and verifies: allowed.
        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.status, W.PAID)
        second = self.submit(amount=100)
        real.approve(both, second, expected_status=W.REQUESTED)
        with self.assertRaises(real.NotAuthorized):
            real.claim(both, second)  # One person can never approve and send.

    def test_permissions_are_assignable_in_the_staff_console(self):
        self.client.force_login(self.root)
        page = self.client.get(reverse("admin:accounts_user_change", args=[self.operator.pk])).content.decode()
        for codename in ("approve_withdrawals", "process_payouts", "verify_payouts", "manage_withdrawal_settings",
                         "verify_withdrawal_identity"):
            permission = Permission.objects.get(content_type__app_label="withdrawals", codename=codename)
            self.assertIn(f'value="{permission.pk}"', page, codename)
        self.assertEqual(self.client.get(reverse("admin:auth_group_add")).status_code, 200)
