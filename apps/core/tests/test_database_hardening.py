"""
RG-03: database functions cannot be redirected through the session search_path.

The trigger functions that guard the ledger, wallets, withdrawals, payments,
rewards, memberships, advertising, viewing, videos, support and audit history
name their tables without a schema. Without a pinned search_path they resolve
those names through the CALLER's path, where PostgreSQL searches the session's
temporary schema first, so a login with TEMPORARY could shadow a protected table
for the duration of its own session. Migration core.0004 pins every one of them.
"""

import uuid

from django.db import DatabaseError, connection, transaction
from django.test import TestCase

from apps.wallets.models import LedgerAccount
from apps.wallets.tests.helpers import LedgerTestCase, check_deferred_now
from apps.wallets.tests.test_database import raw_entry, raw_line

PINNED = "search_path=pg_catalog, public, pg_temp"


def public_functions():
    """(signature, security definer, settings) of every non-extension function in schema public."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p.oid::regprocedure::text, p.prosecdef, coalesce(p.proconfig, '{}') "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' "
            "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e') "
            "ORDER BY 1"
        )
        return cursor.fetchall()


class FunctionSearchPathTests(TestCase):
    def test_every_application_function_pins_a_safe_search_path(self):
        functions = public_functions()
        self.assertGreaterEqual(len(functions), 38)  # Every guard function created by the migrations.
        unpinned = [name for name, _, config in functions if PINNED not in config]
        self.assertEqual(unpinned, [], "functions resolving names through the caller's search_path")

    def test_no_security_definer_functions(self):
        # None is needed: every guard runs with the caller's own privileges.
        self.assertEqual([name for name, definer, _ in public_functions() if definer], [])

    def test_the_migration_covers_every_function(self):
        from importlib import import_module

        listed = set(import_module("apps.core.migrations.0004_pin_function_search_path").FUNCTIONS)
        self.assertEqual(listed, {name for name, _, _ in public_functions()})


class TemporaryShadowTableTests(LedgerTestCase):
    """A session cannot make a protected check read its own look-alike table."""

    def tearDown(self):
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS pg_temp.wallets_journalline")
            cursor.execute("DROP TABLE IF EXISTS pg_temp.wallets_ledgeraccount")
        super().tearDown()

    def test_shadow_lines_cannot_balance_an_unbalanced_entry(self):
        equity = LedgerAccount.objects.get(code="owner_equity")
        with self.assertRaises(DatabaseError), transaction.atomic():
            entry = raw_entry(1_000)
            raw_line(entry, 1, self.cash, "DEBIT", 1_000)  # The only real line: unbalanced.
            with connection.cursor() as cursor:
                cursor.execute("CREATE TEMPORARY TABLE wallets_journalline (LIKE public.wallets_journalline)")
                for number, account, side in ((1, self.cash, "DEBIT"), (2, equity, "CREDIT")):
                    cursor.execute(
                        "INSERT INTO pg_temp.wallets_journalline (id, entry_id, line_number, account_id, side,"
                        " amount, memo) VALUES (%s, %s, %s, %s, %s, 1000, '')",
                        [uuid.uuid4(), entry, number, account.pk, side])
            check_deferred_now()

    def test_shadow_account_cannot_reopen_a_frozen_account(self):
        equity = LedgerAccount.objects.get(code="owner_equity")
        with connection.cursor() as cursor:
            cursor.execute("UPDATE wallets_ledgeraccount SET status = 'FROZEN' WHERE id = %s", [self.cash.pk])
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("CREATE TEMPORARY TABLE wallets_ledgeraccount (LIKE public.wallets_ledgeraccount)")
                cursor.execute("INSERT INTO pg_temp.wallets_ledgeraccount SELECT * FROM public.wallets_ledgeraccount")
                cursor.execute("UPDATE pg_temp.wallets_ledgeraccount SET status = 'ACTIVE'")
            entry = raw_entry(10)
            raw_line(entry, 1, self.cash, "DEBIT", 10)
            raw_line(entry, 2, equity, "CREDIT", 10)
            check_deferred_now()
