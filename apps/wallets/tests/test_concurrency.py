import importlib
import threading
import uuid
from unittest import skipUnless

from django.apps import apps
from django.db import DatabaseError, connection, connections, transaction
from django.test import TransactionTestCase

from apps.wallets.exceptions import InsufficientFundsError
from apps.wallets.models import JournalEntry, LedgerAccount

from .helpers import LedgerFixtures, postings_enabled
from .test_database import raw_entry, raw_line


def run_concurrently(count, func):
    barrier = threading.Barrier(count)
    results = [None] * count

    def worker(index):
        try:
            barrier.wait()
            results[index] = func(index)
        except Exception as error:  # noqa: BLE001 - collected for assertions
            results[index] = error
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


@skipUnless(connection.vendor == "postgresql", "Concurrency is verified on PostgreSQL.")
class ConcurrentPostingTests(LedgerFixtures, TransactionTestCase):
    def setUp(self):
        # TransactionTestCase flushes tables; restore the chart of accounts.
        importlib.import_module("apps.wallets.migrations.0003_platform_accounts").create_platform_accounts(
            apps, None)
        self.make_people()
        self.make_accounts()
        patcher = postings_enabled()
        patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)

    def test_concurrent_allocations_never_exceed_funding(self):
        self.fund_campaign(1_000)
        results = run_concurrently(5, lambda i: self.allocate(300))
        successes = [r for r in results if isinstance(r, JournalEntry)]
        failures = [r for r in results if isinstance(r, InsufficientFundsError)]
        self.assertEqual((len(successes), len(failures)), (3, 2), results)
        self.refresh(self.campaign, self.rewards)
        self.assertEqual((self.campaign.balance, self.rewards.balance), (100, 900))

    def test_concurrent_identical_requests_post_once(self):
        self.fund_campaign(1_000)
        shared_key = f"same-{uuid.uuid4().hex}"
        results = run_concurrently(6, lambda i: self.allocate(250, idempotency_key=shared_key))
        self.assertTrue(all(isinstance(r, JournalEntry) for r in results), results)
        self.assertEqual(len({r.pk for r in results}), 1)
        self.assertEqual(JournalEntry.objects.filter(idempotency_key=shared_key).count(), 1)
        self.refresh(self.campaign)
        self.assertEqual(self.campaign.balance, 750)

    def test_unbalanced_raw_entry_cannot_commit(self):
        equity = LedgerAccount.objects.get(code="owner_equity")
        with self.assertRaises(DatabaseError):
            with transaction.atomic():  # Real COMMIT: deferred checks fire here.
                entry = raw_entry(1_000)
                raw_line(entry, 1, self.cash, "DEBIT", 1_000)
                raw_line(entry, 2, equity, "CREDIT", 400)
        self.assertFalse(JournalEntry.objects.exists())
        self.refresh(self.cash)
        self.assertEqual(self.cash.debit_balance, 0)
