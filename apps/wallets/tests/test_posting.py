import uuid
from decimal import Decimal
from unittest import mock

from django.db import transaction

from apps.audit.context import audit_scope
from apps.audit.models import AuditEvent
from apps.wallets import services
from apps.wallets.exceptions import (
    DuplicateReferenceError,
    IdempotencyConflictError,
    InsufficientFundsError,
    PostingDisabledError,
    PostingNotAuthorizedError,
    PostingValidationError,
)
from apps.wallets.models import JournalEntry, LedgerAccount
from apps.wallets.services import Line, credit, debit

from .helpers import PASSWORD, LedgerTestCase, User, key, postings_enabled


class LockedByDefaultTests(LedgerTestCase):
    def test_every_posting_type_disabled_in_this_phase(self):
        from apps.wallets.posting_types import POSTING_TYPES

        for code in POSTING_TYPES:
            with self.subTest(code=code), audit_scope(), self.assertRaises(PostingDisabledError):
                services.post_entry(code, lines=[debit(self.cash, 1), credit(self.clearing, 1)],
                                    idempotency_key=key(), source_type="test", description="x")
        self.assertFalse(JournalEntry.objects.exists())
        rejected = AuditEvent.objects.filter(action="ledger.posting.rejected")
        self.assertEqual(rejected.count(), len(POSTING_TYPES))
        self.assertEqual(rejected.first().metadata["reason"], "feature_disabled")


class PostingTests(LedgerTestCase):
    def setUp(self):
        super().setUp()
        patcher = postings_enabled()
        patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)

    def test_balanced_posting_updates_balances_and_audits_atomically(self):
        receipt = self.receive(5000)
        self.refresh(self.clearing, self.pending)
        self.assertEqual((self.clearing.balance, self.pending.balance), (5000, 5000))
        self.assertEqual(receipt.lines.count(), 2)
        event = AuditEvent.objects.get(action="ledger.entry.posted")
        self.assertEqual(event.target_id, str(receipt.pk))
        self.assertEqual(event.metadata, {"posting_type": "advertiser.funding_received", "total_amount": 5000,
                                          "line_count": 2, "source_type": "test"})

    def test_invalid_lines_rejected(self):
        cases = {
            "unbalanced": [debit(self.clearing, 100), credit(self.pending, 90)],
            "single line": [debit(self.clearing, 100)],
            "zero": [debit(self.clearing, 0), credit(self.pending, 0)],
            "negative": [debit(self.clearing, -5), credit(self.pending, -5)],
            "float": [debit(self.clearing, 1.5), credit(self.pending, 1.5)],
            "bool": [debit(self.clearing, True), credit(self.pending, True)],
            "fractional decimal": [debit(self.clearing, Decimal("1.5")), credit(self.pending, Decimal("1.5"))],
            "repeated account": [debit(self.clearing, 5), credit(self.clearing, 5)],
            "bad side": [Line(self.clearing, "UP", 5), credit(self.pending, 5)],
            "wrong account types": [debit(self.cash, 5), credit(self.pending, 5)],
        }
        for label, lines in cases.items():
            with self.subTest(label), audit_scope(), self.assertRaises(PostingValidationError):
                services.post_entry("advertiser.funding_received", lines=lines, idempotency_key=key(),
                                    source_type="test", description="x", external_reference=uuid.uuid4().hex)
        self.assertFalse(JournalEntry.objects.exists())
        self.assertEqual(AuditEvent.objects.filter(action="ledger.posting.rejected").count(), len(cases))

    def test_unknown_type_missing_reference_and_missing_key(self):
        lines = [debit(self.clearing, 5), credit(self.pending, 5)]
        for kwargs in ({"posting_type": "advertiser.gift"},
                       {"external_reference": ""},
                       {"idempotency_key": ""}):
            params = {"posting_type": "advertiser.funding_received", "lines": lines, "idempotency_key": key(),
                      "source_type": "test", "description": "x", "external_reference": "R1", **kwargs}
            with self.subTest(kwargs=kwargs), self.assertRaises(PostingValidationError):
                services.post_entry(params.pop("posting_type"), **params)

    def test_unauthorized_actors_rejected(self):
        cm = User.objects.create_user("cm@example.com", PASSWORD, role=User.Role.CONTENT_MANAGER,
                                      status=User.Status.ACTIVE)
        member = User.objects.create_user("m@example.com", PASSWORD, status=User.Status.ACTIVE)
        for actor in (cm, member):
            with self.subTest(actor=actor.email), self.assertRaises(PostingNotAuthorizedError):
                services.post_entry("advertiser.funding_received",
                                    lines=[debit(self.clearing, 5), credit(self.pending, 5)],
                                    idempotency_key=key(), source_type="test", description="x",
                                    external_reference=uuid.uuid4().hex, actor=actor)

    def test_frozen_account_rejected(self):
        services.set_account_status(self.pending, LedgerAccount.Status.FROZEN, actor=self.root)
        with self.assertRaises(PostingValidationError):
            self.receive(100)

    def test_idempotent_replay_and_conflict(self):
        idem = key()
        lines = [debit(self.clearing, 700), credit(self.pending, 700)]
        first = services.post_entry("advertiser.funding_received", lines=lines, idempotency_key=idem,
                                    source_type="test", description="x", external_reference="R-1")
        again = services.post_entry("advertiser.funding_received", lines=lines, idempotency_key=idem,
                                    source_type="test", description="x", external_reference="R-1")
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(JournalEntry.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="ledger.entry.posted").count(), 1)
        self.refresh(self.pending)
        self.assertEqual(self.pending.balance, 700)

        with self.assertRaises(IdempotencyConflictError):
            services.post_entry("advertiser.funding_received",
                                lines=[debit(self.clearing, 701), credit(self.pending, 701)],
                                idempotency_key=idem, source_type="test", description="x", external_reference="R-1")

    def test_duplicate_external_reference_rejected(self):
        self.receive(100, reference="PROV-DUP")
        with self.assertRaises(DuplicateReferenceError):
            self.receive(100, reference="PROV-DUP")
        self.assertEqual(JournalEntry.objects.count(), 1)

    def test_mandatory_audit_failure_rolls_back_posting(self):
        with mock.patch("apps.wallets.services.record_business_event", side_effect=RuntimeError("audit down")):
            with self.assertRaises(RuntimeError):
                self.receive(900)
        self.assertFalse(JournalEntry.objects.exists())
        self.refresh(self.clearing, self.pending)
        self.assertEqual((self.clearing.debit_balance, self.pending.debit_balance), (0, 0))

    def test_caller_rollback_removes_posting(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            self.receive(400)
            raise RuntimeError("caller failed")
        self.assertFalse(JournalEntry.objects.exists())
        self.refresh(self.pending)
        self.assertEqual(self.pending.debit_balance, 0)


class FundingSeparationTests(LedgerTestCase):
    def setUp(self):
        super().setUp()
        patcher = postings_enabled()
        patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)

    def test_full_funding_path(self):
        self.fund_campaign(10_000)
        self.allocate(2_500)
        self.refresh(self.campaign, self.rewards, self.verified, self.pending)
        self.assertEqual((self.pending.balance, self.verified.balance, self.campaign.balance, self.rewards.balance),
                         (0, 0, 7_500, 2_500))

    def test_unverified_funds_cannot_reach_campaign(self):
        self.receive(1_000)
        with self.assertRaises(PostingValidationError):
            services.post_entry("campaign.funding_committed",
                                lines=[debit(self.pending, 1_000), credit(self.campaign, 1_000)],
                                idempotency_key=key(), source_type="test", description="x", actor=self.root)

    def test_membership_fees_cannot_fund_campaigns_or_rewards(self):
        services.post_entry("membership.payment_received",
                            lines=[debit(self.clearing, 3_000), credit(self.deferred, 3_000)],
                            idempotency_key=key(), source_type="test", description="fee", external_reference="M-1")
        for posting_type, lines in (
            ("campaign.funding_committed", [debit(self.deferred, 1_000), credit(self.campaign, 1_000)]),
            ("reward.allocated", [debit(self.deferred, 1_000), credit(self.rewards, 1_000)]),
            ("reward.allocated", [debit(self.membership_revenue, 1_000), credit(self.rewards, 1_000)]),
        ):
            with self.subTest(posting_type=posting_type), self.assertRaises(PostingValidationError):
                services.post_entry(posting_type, lines=lines, idempotency_key=key(), source_type="test",
                                    description="x", actor=self.root)
        self.refresh(self.campaign, self.rewards)
        self.assertEqual((self.campaign.balance, self.rewards.balance), (0, 0))

    def test_allocation_cannot_exceed_campaign_funding(self):
        self.fund_campaign(1_000)
        self.allocate(600)
        with self.assertRaises(InsufficientFundsError):
            self.allocate(401)
        self.allocate(400)
        self.refresh(self.campaign)
        self.assertEqual(self.campaign.balance, 0)
        with self.assertRaises(InsufficientFundsError):
            self.allocate(1)

    def test_funds_cannot_be_committed_twice(self):
        self.verify(self.receive(1_000))
        self.commit(1_000)
        other_campaign = services.open_account("CAMPAIGN_RESTRICTED_FUNDING", scope_id=str(uuid.uuid4()))
        with self.assertRaises(InsufficientFundsError):
            services.post_entry("campaign.funding_committed",
                                lines=[debit(self.verified, 1_000), credit(other_campaign, 1_000)],
                                idempotency_key=key(), source_type="test", description="x", actor=self.root)

    def test_verification_rules(self):
        receipt = self.receive(2_000)
        with self.subTest("same person who recorded the receipt"):
            own_receipt = services.post_entry(
                "advertiser.funding_received", lines=[debit(self.clearing, 50), credit(self.pending, 50)],
                idempotency_key=key(), source_type="test", description="x", external_reference="OWN-1",
                actor=self.root)
            with self.assertRaises(PostingNotAuthorizedError):
                self.verify(own_receipt, actor=self.root)
        with self.subTest("system cannot verify"), self.assertRaises(PostingNotAuthorizedError):
            services.post_entry("advertiser.funding_verified",
                                lines=[debit(self.pending, 2_000), credit(self.verified, 2_000)],
                                idempotency_key=key(), source_type="test", description="x", related_entry=receipt)
        with self.subTest("amount must match"), self.assertRaises(PostingValidationError):
            self.verify(receipt, amount=1_999)
        with self.subTest("other advertiser"), self.assertRaises(PostingValidationError):
            other_verified = services.open_account("ADVERTISER_FUNDING_VERIFIED", scope_id=str(uuid.uuid4()))
            services.post_entry("advertiser.funding_verified",
                                lines=[debit(self.pending, 2_000), credit(other_verified, 2_000)],
                                idempotency_key=key(), source_type="test", description="x",
                                related_entry=receipt, actor=self.verifier)
        with self.subTest("missing related entry"), self.assertRaises(PostingValidationError):
            services.post_entry("advertiser.funding_verified",
                                lines=[debit(self.pending, 10), credit(self.verified, 10)],
                                idempotency_key=key(), source_type="test", description="x", actor=self.verifier)

        self.verify(receipt)
        with self.subTest("verified only once"), self.assertRaises(DuplicateReferenceError):
            self.verify(receipt)

    def test_withdrawal_cannot_be_paid_twice(self):
        self.fund_campaign(1_000)
        self.allocate(500)
        approval = services.post_entry(
            "withdrawal.approved", lines=[debit(self.rewards, 500), credit(self.withdrawal_clearing, 500)],
            idempotency_key=key(), source_type="test", description="approve", actor=self.root)

        def pay(reference):
            return services.post_entry(
                "withdrawal.paid", lines=[debit(self.withdrawal_clearing, 500), credit(self.clearing, 500)],
                idempotency_key=key(), source_type="test", description="pay", external_reference=reference,
                related_entry=approval)

        pay("PAYOUT-1")
        with self.assertRaises(DuplicateReferenceError):  # Approval already paid.
            pay("PAYOUT-2")
        with self.assertRaises(DuplicateReferenceError):
            pay("PAYOUT-1")
        self.refresh(self.withdrawal_clearing, self.rewards)
        self.assertEqual((self.withdrawal_clearing.balance, self.rewards.balance), (0, 0))

    def test_reward_liability_cannot_be_withdrawn_twice(self):
        self.fund_campaign(300)
        self.allocate(300)
        services.post_entry("withdrawal.approved",
                            lines=[debit(self.rewards, 300), credit(self.withdrawal_clearing, 300)],
                            idempotency_key=key(), source_type="test", description="x", actor=self.root)
        with self.assertRaises(InsufficientFundsError):
            services.post_entry("withdrawal.approved",
                                lines=[debit(self.rewards, 300), credit(self.withdrawal_clearing, 300)],
                                idempotency_key=key(), source_type="test", description="x", actor=self.root)


class AccountStatusTests(LedgerTestCase):
    def test_status_changes(self):
        services.set_account_status(self.campaign, LedgerAccount.Status.FROZEN, actor=self.root)
        services.set_account_status(self.campaign, LedgerAccount.Status.ACTIVE, actor=self.root)
        services.set_account_status(self.campaign, LedgerAccount.Status.CLOSED, actor=self.root)
        with self.assertRaises(PostingValidationError):
            services.set_account_status(self.campaign, LedgerAccount.Status.ACTIVE, actor=self.root)
        self.assertEqual(AuditEvent.objects.filter(action="ledger.account.status_changed").count(), 3)

    def test_cannot_close_with_balance_or_without_authority(self):
        with postings_enabled():
            self.receive(10)
        with self.assertRaises(PostingValidationError):
            services.set_account_status(self.pending, LedgerAccount.Status.CLOSED, actor=self.root)
        cm = User.objects.create_user("cm@example.com", PASSWORD, role=User.Role.CONTENT_MANAGER,
                                      status=User.Status.ACTIVE)
        for actor in (None, cm):
            with self.subTest(actor=actor), self.assertRaises(PostingNotAuthorizedError):
                services.set_account_status(self.pending, LedgerAccount.Status.FROZEN, actor=actor)
