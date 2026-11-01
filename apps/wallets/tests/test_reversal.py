from apps.audit.models import AuditEvent
from apps.wallets import services
from apps.wallets.exceptions import (
    InsufficientFundsError,
    PostingDisabledError,
    PostingNotAuthorizedError,
    PostingValidationError,
    ReversalError,
)

from .helpers import PASSWORD, LedgerTestCase, User, key, postings_enabled


class ReversalTests(LedgerTestCase):
    def setUp(self):
        super().setUp()
        patcher = postings_enabled()
        patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)

    def test_reversal_mirrors_and_restores_balances(self):
        receipt = self.receive(800)
        reversal = services.reverse_entry(receipt, idempotency_key=key(), reason="Duplicate webhook",
                                          actor=self.root)
        self.assertEqual(reversal.reverses_id, receipt.pk)
        self.assertEqual(receipt.reversed_by, reversal)
        mirrored = {(l.account_id, l.side, l.amount) for l in reversal.lines.all()}
        original = {(l.account_id, "CREDIT" if l.side == "DEBIT" else "DEBIT", l.amount) for l in receipt.lines.all()}
        self.assertEqual(mirrored, original)
        self.refresh(self.clearing, self.pending)
        self.assertEqual((self.clearing.debit_balance, self.pending.debit_balance), (0, 0))
        event = AuditEvent.objects.get(action="ledger.entry.reversed")
        self.assertEqual(event.metadata["original_entry"], str(receipt.pk))

    def test_cannot_reverse_twice_or_reverse_a_reversal(self):
        receipt = self.receive(800)
        reversal = services.reverse_entry(receipt, idempotency_key=key(), reason="r", actor=self.root)
        with self.assertRaises(ReversalError):
            services.reverse_entry(receipt, idempotency_key=key(), reason="again", actor=self.root)
        with self.assertRaises(ReversalError):
            services.reverse_entry(reversal, idempotency_key=key(), reason="undo", actor=self.root)

    def test_idempotent_reversal_replay(self):
        receipt = self.receive(800)
        idem = key()
        first = services.reverse_entry(receipt, idempotency_key=idem, reason="r", actor=self.root)
        second = services.reverse_entry(receipt, idempotency_key=idem, reason="r", actor=self.root)
        self.assertEqual(first.pk, second.pk)

    def test_cannot_reverse_spent_funds(self):
        self.fund_campaign(1_000)
        allocation = self.allocate(1_000)
        commitment = allocation.lines.get(side="DEBIT").account.lines.get(side="CREDIT").entry
        with self.assertRaises(InsufficientFundsError):
            services.reverse_entry(commitment, idempotency_key=key(), reason="undo funding", actor=self.root)
        services.reverse_entry(allocation, idempotency_key=key(), reason="reward withdrawn", actor=self.root)
        services.reverse_entry(commitment, idempotency_key=key(), reason="undo funding", actor=self.root)
        self.refresh(self.campaign, self.verified, self.rewards)
        self.assertEqual((self.campaign.balance, self.verified.balance, self.rewards.balance), (0, 1_000, 0))

    def test_reversed_receipt_cannot_be_verified(self):
        receipt = self.receive(300)
        services.reverse_entry(receipt, idempotency_key=key(), reason="bounced", actor=self.root)
        with self.assertRaises(PostingValidationError):
            self.verify(receipt)

    def test_authorization_and_reason_required(self):
        receipt = self.receive(100)
        cm = User.objects.create_user("cm@example.com", PASSWORD, role=User.Role.CONTENT_MANAGER,
                                      status=User.Status.ACTIVE)
        for actor in (None, cm):
            with self.subTest(actor=actor), self.assertRaises(PostingNotAuthorizedError):
                services.reverse_entry(receipt, idempotency_key=key(), reason="r", actor=actor)
        with self.assertRaises(ReversalError):
            services.reverse_entry(receipt, idempotency_key=key(), reason="  ", actor=self.root)


class ReversalLockedTests(LedgerTestCase):
    def test_reversals_disabled_with_their_feature(self):
        with postings_enabled():
            receipt = self.receive(100)
        with self.assertRaises(PostingDisabledError):
            services.reverse_entry(receipt, idempotency_key=key(), reason="r", actor=self.root)
