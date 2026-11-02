import re
import uuid
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, transaction
from django.test import TestCase

from apps.accounts.tests.helpers import make_user, run_worker
from apps.audit.models import AuditEvent
from apps.core.jobs.services import enqueue
from apps.wallets import services as ledger
from apps.wallets import wallet_services
from apps.wallets.exceptions import LedgerError
from apps.wallets.models import JournalEntry, Wallet, WalletDiscrepancy, WalletRestriction, WalletTransaction

from .helpers import LedgerFixtures, User, key, postings_enabled


class WalletTestCase(TestCase):
    def setUp(self):
        self.member = make_user("member@example.com")
        self.other = make_user("other@example.com")
        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.second = make_user("second@example.com", role=User.Role.SUPER_ADMIN)
        self.manager = make_user("cm@example.com", role=User.Role.CONTENT_MANAGER)


class IdentityTests(WalletTestCase):
    def test_one_wallet_per_member_with_three_empty_accounts(self):
        wallet = wallet_services.ensure_wallet(self.member)
        self.assertEqual(wallet_services.ensure_wallet(self.member).pk, wallet.pk)
        self.assertRegex(wallet.number, r"^CW-[A-Z2-9]{10}$")
        self.assertEqual((wallet.available_account.account_type, wallet.pending_account.account_type,
                          wallet.reserved_account.account_type),
                         ("REWARD_LIABILITY", "MEMBER_REWARD_PENDING", "MEMBER_WITHDRAWAL_RESERVED"))
        for account in (wallet.available_account, wallet.pending_account, wallet.reserved_account):
            self.assertEqual((account.scope_id, account.balance), (str(self.member.pk), 0))
        self.assertEqual(AuditEvent.objects.filter(action="wallets.wallet.created").count(), 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Wallet.objects.create(member=self.member, available_account=wallet.available_account,
                                  pending_account=wallet.pending_account, reserved_account=wallet.reserved_account)

    def test_staff_and_ineligible_accounts_get_no_wallet(self):
        unverified = make_user("u@example.com", verified=False)
        for user, reason in ((self.root, "staff_account"), (self.manager, "staff_account"),
                             (unverified, "email_unverified")):
            with self.subTest(user=user.email), self.assertRaises(wallet_services.NotEligible) as caught:
                wallet_services.ensure_wallet(user)
            self.assertEqual(caught.exception.code, reason)
        self.assertFalse(Wallet.objects.exists())

    def test_identity_is_immutable_and_linking_is_checked(self):
        wallet = wallet_services.ensure_wallet(self.member)
        other_wallet = wallet_services.ensure_wallet(self.other)
        for change in ({"number": "CW-AAAAAAAAAA"}, {"member": self.other}, {"currency": "USD"}):
            with self.subTest(change=change), self.assertRaises((DatabaseError, IntegrityError)), \
                    transaction.atomic():
                Wallet.objects.filter(pk=wallet.pk).update(**change)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Wallet.objects.filter(pk=wallet.pk).delete()
        third = make_user("third@example.com")
        foreign = ledger.open_account("REWARD_LIABILITY", scope_id=str(other_wallet.member_id) + "x")
        with self.assertRaises(DatabaseError), transaction.atomic():  # Someone else's / wrong accounts.
            Wallet.objects.create(member=third, available_account=foreign,
                                  pending_account=ledger.open_account("MEMBER_REWARD_PENDING", scope_id=str(third.pk)),
                                  reserved_account=ledger.open_account("MEMBER_WITHDRAWAL_RESERVED",
                                                                       scope_id=str(third.pk)))


class ZeroMoneyTests(LedgerFixtures, WalletTestCase):
    def test_balances_are_zero_and_projection_uses_ledger_signs(self):
        wallet = wallet_services.ensure_wallet(self.member)
        balances = wallet_services.balances(wallet)
        self.assertEqual((balances.available, balances.pending, balances.reserved, balances.spendable), (0, 0, 0, 0))
        # Sign convention check on a ledger account that is NOT a wallet (Phase 02C fixtures).
        self.make_people_for_ledger()
        with postings_enabled():
            self.fund_campaign(1_000)
            self.allocate(400)
        self.rewards.refresh_from_db()
        self.assertEqual(wallet_services.account_balances([self.rewards.pk])[self.rewards.pk], 400)
        self.assertEqual(self.rewards.balance, 400)

    def make_people_for_ledger(self):
        self.verifier = self.second
        self.make_accounts()

    def test_database_refuses_postings_to_wallet_accounts_even_with_gates_open(self):
        wallet = wallet_services.ensure_wallet(self.member)
        self.make_people_for_ledger()
        self.rewards = wallet.available_account  # Point the reward posting at the member's wallet.
        before = JournalEntry.objects.count()
        with postings_enabled():
            self.fund_campaign(1_000)
            with self.assertRaises((LedgerError, DatabaseError)):
                self.allocate(100)
        self.assertEqual(JournalEntry.objects.count(), before + 3)  # Funding only; no wallet posting.
        self.assertEqual(wallet_services.balances(wallet).available, 0)

    def test_posting_entry_point_always_refuses(self):
        wallet = wallet_services.ensure_wallet(self.member)
        for category in WalletTransaction.Category.values:
            with self.subTest(category=category), self.assertRaises(wallet_services.WalletPostingLocked):
                wallet_services.request_wallet_posting(category, wallet, 100, actor=self.root)
        self.assertEqual(AuditEvent.objects.filter(action="wallets.posting.refused").count(),
                         len(WalletTransaction.Category.values))
        entry = None
        self.make_people_for_ledger()
        with postings_enabled():
            entry = self.receive(500)
        with self.assertRaises(DatabaseError), transaction.atomic():
            WalletTransaction.objects.create(wallet=wallet, journal_entry=entry, reference="WT-1",
                                             idempotency_key=key(), category="REWARD_CREDIT", bucket="AVAILABLE",
                                             direction="IN", amount=100)


class RestrictionTests(WalletTestCase):
    def setUp(self):
        super().setUp()
        self.wallet = wallet_services.ensure_wallet(self.member)

    def test_two_person_lift(self):
        with self.assertRaises(wallet_services.NotAuthorized):
            wallet_services.place_restriction(self.manager, self.wallet, kind="FRAUD_REVIEW", reason="Suspicious")
        with self.assertRaises(ValidationError):
            wallet_services.place_restriction(self.root, self.wallet, kind="FRAUD_REVIEW", reason="")
        restriction = wallet_services.place_restriction(self.root, self.wallet, kind="FRAUD_REVIEW",
                                                        reason="Multiple flagged views")
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.status, "RESTRICTED")
        self.assertEqual(wallet_services.balances(self.wallet).spendable, 0)
        wallet_services.request_lift(self.root, restriction, reason="Review complete")
        with self.assertRaises(wallet_services.NotAuthorized) as caught:
            wallet_services.decide_lift(self.root, restriction, approve=True)
        self.assertEqual(caught.exception.code, "four_eyes")
        wallet_services.decide_lift(self.second, restriction, approve=True, note="Agreed")
        self.wallet.refresh_from_db()
        restriction.refresh_from_db()
        self.assertEqual((self.wallet.status, restriction.status, restriction.lifted_by), ("ACTIVE", "LIFTED", self.second))
        for action in ("wallets.restriction.placed", "wallets.restriction.lift_requested", "wallets.restriction.lifted"):
            self.assertTrue(AuditEvent.objects.filter(action=action).exists())
        with self.assertRaises(DatabaseError), transaction.atomic():
            WalletRestriction.objects.filter(pk=restriction.pk).update(status="ACTIVE")
        with self.assertRaises(IntegrityError), transaction.atomic():
            WalletRestriction.objects.filter(pk=restriction.pk).update(lifted_by=self.root)

    def test_declined_lift_keeps_restriction(self):
        restriction = wallet_services.place_restriction(self.root, self.wallet, kind="SECURITY_HOLD", reason="Login abuse")
        wallet_services.request_lift(self.second, restriction, reason="Seems fine")
        wallet_services.decide_lift(self.root, restriction, approve=False, note="Not yet")
        restriction.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual((restriction.status, self.wallet.status), ("ACTIVE", "RESTRICTED"))


class ReconciliationTests(WalletTestCase):
    def test_clean_wallets_have_no_findings(self):
        wallet_services.ensure_wallet(self.member)
        self.assertEqual(wallet_services.reconcile(), {"wallets": 1, "findings": 0})

    def test_findings_are_reported_once_and_never_auto_fixed(self):
        wallet = wallet_services.ensure_wallet(self.member)
        Wallet.objects.filter(pk=wallet.pk).update(status="RESTRICTED")  # No restriction exists.
        ledger.open_account("REWARD_LIABILITY", scope_id=str(self.other.pk))  # Orphan member account.
        third = make_user("third@example.com")
        accounts = {b: ledger.open_account(t, scope_id=str(third.pk))
                    for b, t in wallet_services.WALLET_ACCOUNT_TYPES.items()}
        Wallet.objects.create(member=third, available_account=accounts["available"],  # Bypasses the service.
                              pending_account=accounts["pending"], reserved_account=accounts["reserved"])
        summary = wallet_services.reconcile()
        codes = set(WalletDiscrepancy.objects.values_list("code", flat=True))
        self.assertEqual(codes, {"status_mismatch", "orphan_member_account", "missing_audit"})
        self.assertEqual(summary["findings"], 3)
        wallet_services.reconcile()
        self.assertEqual(WalletDiscrepancy.objects.count(), 3)  # Idempotent.
        wallet.refresh_from_db()
        self.assertEqual(wallet.status, "RESTRICTED")  # Reported, not corrected.
        finding = WalletDiscrepancy.objects.get(code="status_mismatch")
        with self.assertRaises(wallet_services.NotAuthorized):
            wallet_services.resolve_discrepancy(self.member, finding, note="Me")
        wallet_services.resolve_discrepancy(self.root, finding, note="Investigated: test data")
        self.assertEqual(AuditEvent.objects.filter(action="wallets.reconciliation.discrepancy").count(), 3)

    def test_job_and_command(self):
        wallet_services.ensure_wallet(self.member)
        enqueue("wallets.reconcile", {})
        run_worker()
        enqueue("wallets.reconcile", {}, idempotency_key="again")
        run_worker()
        self.assertFalse(WalletDiscrepancy.objects.exists())
        out = StringIO()
        call_command("reconcile_wallets", stdout=out)
        self.assertIn("Checked 1 wallets; 0 findings.", out.getvalue())


class SeparationTests(WalletTestCase):
    def test_audit_has_no_personal_data(self):
        wallet = wallet_services.ensure_wallet(self.member)
        wallet_services.place_restriction(self.root, wallet, kind="COMPLIANCE", reason="Document check")
        for event in AuditEvent.objects.filter(category="WALLETS"):
            self.assertNotIn("@", str(event.metadata))
            self.assertIsNone(re.search(r"\d{9,}", str(event.metadata)))

    def test_inspection_requires_permission_and_is_audited(self):
        wallet = wallet_services.ensure_wallet(self.member)
        with self.assertRaises(wallet_services.NotAuthorized):
            wallet_services.wallet_for_staff(self.manager, wallet.pk)
        with self.assertRaises(wallet_services.NotEligible):
            wallet_services.wallet_for_staff(self.root, uuid.uuid4())
        wallet_services.wallet_for_staff(self.root, wallet.pk)
        self.assertTrue(AuditEvent.objects.filter(action="wallets.wallet.inspected", target_id=str(wallet.pk)).exists())
        self.assertTrue(AuditEvent.objects.filter(action="wallets.access.denied").exists())
