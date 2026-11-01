"""
Direct-SQL bypass attempts: the database must reject what the service would,
even when Django's posting service is not used at all.
"""

import uuid

from django.db import DatabaseError, connection, transaction

from apps.wallets.models import JournalEntry, LedgerAccount

from .helpers import LedgerTestCase, check_deferred_now, postings_enabled


def raw_entry(total, *, reverses=None, posting_type="test.raw"):
    entry_id = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO wallets_journalentry (id, posting_type, status, description, currency, total_amount,"
            " posted_at, source_type, source_id, external_reference, related_entry_id, reverses_id,"
            " idempotency_key, fingerprint, actor_id, request_id)"
            " VALUES (%s, %s, 'POSTED', 'raw', 'TZS', %s, now(), 'raw', '', '', NULL, %s, %s, 'x', NULL, '')",
            [entry_id, posting_type, total, reverses, f"raw-{entry_id}"],
        )
    return entry_id


def raw_line(entry_id, number, account, side, amount):
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO wallets_journalline (id, entry_id, line_number, account_id, side, amount, memo)"
            " VALUES (%s, %s, %s, %s, %s, %s, '')",
            [uuid.uuid4(), entry_id, number, account.pk, side, amount],
        )


class DirectSqlBypassTests(LedgerTestCase):
    def assert_rejected(self, build):
        """``build`` writes rows; the database must refuse them by commit time."""
        with self.assertRaises(DatabaseError), transaction.atomic():
            build()
            check_deferred_now()

    def test_valid_raw_entry_is_accepted_and_balances_maintained_by_database(self):
        equity = LedgerAccount.objects.get(code="owner_equity")
        entry = raw_entry(1_000)
        raw_line(entry, 1, self.cash, "DEBIT", 1_000)
        raw_line(entry, 2, equity, "CREDIT", 1_000)
        check_deferred_now()
        self.refresh(self.cash, equity)
        self.assertEqual((self.cash.balance, equity.balance), (1_000, 1_000))

    def test_unbalanced_entry(self):
        equity = LedgerAccount.objects.get(code="owner_equity")

        def build():
            entry = raw_entry(1_000)
            raw_line(entry, 1, self.cash, "DEBIT", 1_000)
            raw_line(entry, 2, equity, "CREDIT", 999)

        self.assert_rejected(build)

    def test_single_line_and_empty_entries(self):
        self.assert_rejected(lambda: raw_line(raw_entry(10), 1, self.cash, "DEBIT", 10))
        self.assert_rejected(lambda: raw_entry(10))

    def test_total_mismatch(self):
        equity = LedgerAccount.objects.get(code="owner_equity")

        def build():
            entry = raw_entry(5)
            raw_line(entry, 1, self.cash, "DEBIT", 10)
            raw_line(entry, 2, equity, "CREDIT", 10)

        self.assert_rejected(build)

    def test_invalid_amounts(self):
        equity = LedgerAccount.objects.get(code="owner_equity")
        for amount in (0, -10):
            with self.subTest(amount=amount):
                def build(amount=amount):
                    entry = raw_entry(10)
                    raw_line(entry, 1, self.cash, "DEBIT", amount)
                    raw_line(entry, 2, equity, "CREDIT", amount)

                self.assert_rejected(build)
        self.assert_rejected(lambda: raw_entry(0))

    def test_membership_and_reward_funds_never_mix(self):
        def build():
            entry = raw_entry(100)
            raw_line(entry, 1, self.deferred, "DEBIT", 100)
            raw_line(entry, 2, self.campaign, "CREDIT", 100)

        self.assert_rejected(build)

    def test_rewards_cannot_be_funded_from_cash(self):
        def build():
            entry = raw_entry(100)
            raw_line(entry, 1, self.cash, "DEBIT", 100)
            raw_line(entry, 2, self.campaign, "CREDIT", 100)

        self.assert_rejected(build)

    def test_verified_funds_only_from_pending(self):
        def build():
            entry = raw_entry(100)
            raw_line(entry, 1, self.clearing, "DEBIT", 100)
            raw_line(entry, 2, self.verified, "CREDIT", 100)

        self.assert_rejected(build)

    def test_overdraft_rejected_immediately(self):
        def build():
            entry = raw_entry(100)
            raw_line(entry, 1, self.campaign, "DEBIT", 100)  # Campaign has no funds.
            raw_line(entry, 2, self.rewards, "CREDIT", 100)

        self.assert_rejected(build)

    def test_posting_to_frozen_account(self):
        with connection.cursor() as cursor:
            cursor.execute("UPDATE wallets_ledgeraccount SET status = 'FROZEN' WHERE id = %s", [self.cash.pk])
        equity = LedgerAccount.objects.get(code="owner_equity")

        def build():
            entry = raw_entry(10)
            raw_line(entry, 1, self.cash, "DEBIT", 10)
            raw_line(entry, 2, equity, "CREDIT", 10)

        self.assert_rejected(build)

    def test_balances_and_account_identity_cannot_be_edited(self):
        for sql in (
            "UPDATE wallets_ledgeraccount SET debit_balance = 1000000 WHERE id = %s",
            "UPDATE wallets_ledgeraccount SET account_type = 'CASH' WHERE id = %s",
            "UPDATE wallets_ledgeraccount SET funding_class = 'ADVERTISER_VERIFIED' WHERE id = %s",
            "DELETE FROM wallets_ledgeraccount WHERE id = %s",
        ):
            with self.subTest(sql=sql), self.assertRaises(DatabaseError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(sql, [self.campaign.pk])
        self.refresh(self.campaign)
        self.assertEqual(self.campaign.debit_balance, 0)

    def test_posted_rows_cannot_be_modified_or_deleted(self):
        with postings_enabled():
            receipt = self.receive(500)
        for sql in (
            "UPDATE wallets_journalentry SET total_amount = 1 WHERE id = %s",
            "DELETE FROM wallets_journalentry WHERE id = %s",
            "UPDATE wallets_journalline SET amount = 1 WHERE entry_id = %s",
            "DELETE FROM wallets_journalline WHERE entry_id = %s",
        ):
            with self.subTest(sql=sql), self.assertRaises(DatabaseError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(sql, [receipt.pk])
        self.assertEqual(JournalEntry.objects.get(pk=receipt.pk).lines.count(), 2)

    def test_orm_modification_blocked(self):
        from apps.wallets.models import LedgerImmutableError

        with postings_enabled():
            receipt = self.receive(500)
        for operation in (receipt.save, receipt.delete, lambda: JournalEntry.objects.all().delete(),
                          lambda: JournalEntry.objects.update(description="x"),
                          lambda: receipt.lines.first().save(), lambda: receipt.lines.all().delete()):
            with self.subTest(operation=operation), self.assertRaises(LedgerImmutableError):
                operation()

    def test_reversal_rules_enforced_by_database(self):
        with postings_enabled():
            receipt = self.receive(500)

        def not_a_mirror():
            entry = raw_entry(400, reverses=receipt.pk, posting_type=receipt.posting_type)
            raw_line(entry, 1, self.pending, "DEBIT", 400)
            raw_line(entry, 2, self.clearing, "CREDIT", 400)

        def same_direction():
            entry = raw_entry(500, reverses=receipt.pk, posting_type=receipt.posting_type)
            raw_line(entry, 1, self.clearing, "DEBIT", 500)
            raw_line(entry, 2, self.pending, "CREDIT", 500)

        self.assert_rejected(not_a_mirror)
        self.assert_rejected(same_direction)

        def mirror():
            entry = raw_entry(500, reverses=receipt.pk, posting_type=receipt.posting_type)
            raw_line(entry, 1, self.pending, "DEBIT", 500)
            raw_line(entry, 2, self.clearing, "CREDIT", 500)
            return entry

        reversal = mirror()
        check_deferred_now()
        self.assert_rejected(mirror)  # Second reversal of the same entry.

        def reverse_the_reversal():
            entry = raw_entry(500, reverses=reversal, posting_type=receipt.posting_type)
            raw_line(entry, 1, self.clearing, "DEBIT", 500)
            raw_line(entry, 2, self.pending, "CREDIT", 500)

        self.assert_rejected(reverse_the_reversal)
