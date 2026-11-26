"""
DB-01: ClipCash's database safeguards on MariaDB.

Runs only on MariaDB (DB_ENGINE=mariadb, ClipCash's backend); skipped on PostgreSQL,
where the original suite proves the same rules. Covers what the first port phase
implements: every uniqueness rule Django would otherwise skip on MariaDB,
case-insensitive email and unique phone numbers, single-use Airtel references with one
approval and one activation per membership, campaign demo budgets under concurrent
awards, and immutable viewing sessions; the ledger phase adds the explicit ledger
finalization step (draft -> posted), Airtel approval through the real service, and the
separate migration and runtime logins. The PostgreSQL concurrency suites for these areas
are re-run here on real MariaDB connections (one per thread).
"""

import datetime as dt
import re
import uuid
from datetime import timedelta
from importlib import import_module
from io import StringIO
from unittest import mock, skipUnless

from django.apps import apps
from django.conf import settings
from django.db import DatabaseError, IntegrityError, connection, connections, transaction
from django.core.management import call_command
from django.db.migrations.loader import MigrationLoader
from django.db.models import UniqueConstraint
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone

from apps.accounts.tests.helpers import User, make_user
from apps.audit.models import AuditEvent
from apps.core.mariadb_logins import (
    migration_login_findings,
    parse_grant,
    parse_role_grants,
    runtime_login_findings,
)
from apps.core.migration_ops import MariaDBRunSQL, is_mariadb
from apps.memberships import services as memberships
from apps.memberships.models import Subscription
from apps.memberships.tests import test_concurrency as membership_concurrency
from apps.memberships.tests import test_complimentary as complimentary_concurrency
from apps.memberships.tests.test_concurrency import run_threads
from apps.payments import manual
from apps.payments.models import ManualMembershipPayment, new_manual_reference
from apps.payments.tests import test_manual_concurrency as manual_concurrency
from apps.payments.tests.manual_helpers import ManualPaymentTestCase, airtel_reference
from apps.rewards.models import CampaignRewardBudget
from apps.rewards.tests import test_concurrency as reward_concurrency
from apps.rewards.tests import test_credit_migration as credit_migration
from apps.rewards.tests import test_credits as credits
from apps.viewing.models import WatchSession
from apps.wallets import services as ledger
from apps.wallets.models import JournalEntry
from config.db_backends.mariadb.schema import needs_emulation

ON_MARIADB = is_mariadb(connection)
mariadb_only = skipUnless(ON_MARIADB, "MariaDB port (DB-01) only.")

GUARD_TRIGGERS = {
    "advertising_campaign_delete", "advertising_campaign_insert", "advertising_campaign_update",
    "advertising_invitation_delete", "advertising_membership_delete", "advertising_organization_delete",
    "advertising_organization_insert", "advertising_organization_update", "advertising_video_grant_delete",
    "membership_plan_delete", "membership_plan_update", "membership_plan_version_delete",
    "membership_plan_version_update", "membership_subscription_delete", "membership_subscription_insert",
    "membership_subscription_update", "payments_manual_claim_delete", "payments_manual_claim_insert",
    "payments_manual_claim_update", "payments_manual_instruction_delete", "payments_manual_instruction_insert",
    "payments_manual_instruction_update", "rewards_budget_delete", "rewards_budget_insert", "rewards_budget_update",
    "rewards_demo_reward_delete", "rewards_demo_reward_insert", "rewards_demo_reward_update",
    "rewards_policy_delete", "rewards_policy_insert", "rewards_policy_update", "viewing_session_delete",
    "viewing_session_insert", "viewing_session_update",
    # CR-01 credit terms (rewards 0004).
    "rewards_campaign_budget_bounds", "rewards_credit_terms_insert", "rewards_credit_terms_update",
    # Ledger phase: finalization (wallets 0002) and the wallet guards (wallets 0005).
    "ledger_account_delete", "ledger_account_insert", "ledger_account_update", "ledger_entry_delete",
    "ledger_entry_finalize_apply", "ledger_entry_finalize_check", "ledger_entry_insert", "ledger_line_delete",
    "ledger_line_insert", "ledger_line_update", "wallets_block_member_postings", "wallets_discrepancy_delete",
    "wallets_discrepancy_update", "wallets_restriction_delete", "wallets_restriction_insert",
    "wallets_restriction_update", "wallets_transactions_locked_delete", "wallets_transactions_locked_insert",
    "wallets_transactions_locked_update", "wallets_wallet_delete", "wallets_wallet_insert", "wallets_wallet_update",
}


def refused(test, error=DatabaseError):
    """assertRaises in its own savepoint, so the test transaction stays usable."""
    return _Refused(test, error)


class _Refused:
    def __init__(self, test, error):
        self.raises = test.assertRaises(error)
        self.atomic = transaction.atomic()

    def __enter__(self):
        self.raises.__enter__()
        self.atomic.__enter__()

    def __exit__(self, *exc):
        self.atomic.__exit__(*exc)
        return self.raises.__exit__(*exc)


def clone(instance, **changes):
    """Insert a copy of ``instance`` with a new primary key (and ``changes``) directly,
    bypassing the services, as a buggy or hostile writer would."""
    model = type(instance)
    values = {field.attname: getattr(instance, field.attname) for field in model._meta.concrete_fields
              if not field.primary_key}
    values.update(changes)
    return model.objects.create(pk=uuid.uuid4(), **values)


@mariadb_only
class SchemaTests(TestCase):
    def test_every_emulated_unique_constraint_is_a_unique_index(self):
        wanted = {}
        for model in apps.get_models():
            for constraint in model._meta.constraints:
                if isinstance(constraint, UniqueConstraint) and needs_emulation(
                        constraint.condition, constraint.expressions, constraint.nulls_distinct):
                    parts = len(constraint.fields) + len(constraint.expressions)
                    wanted[(model._meta.db_table, constraint.name)] = parts
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT s.TABLE_NAME, s.INDEX_NAME, COUNT(*), SUM(c.EXTRA LIKE '%%STORED GENERATED%%') "
                "FROM information_schema.STATISTICS s JOIN information_schema.COLUMNS c "
                "ON c.TABLE_SCHEMA = s.TABLE_SCHEMA AND c.TABLE_NAME = s.TABLE_NAME AND c.COLUMN_NAME = s.COLUMN_NAME "
                "WHERE s.TABLE_SCHEMA = DATABASE() AND s.NON_UNIQUE = 0 GROUP BY 1, 2")
            unique = {(table, name): (count, generated) for table, name, count, generated in cursor.fetchall()}
        self.assertGreaterEqual(len(wanted), 32)  # Partial, functional and NULLS NOT DISTINCT rules.
        for key, parts in wanted.items():
            self.assertIn(key, unique, f"{key} has no unique index")
            count, generated = unique[key]
            self.assertEqual(count, generated, f"{key} must index only generated key columns")
            self.assertGreaterEqual(count, parts, key)

    def test_guard_triggers_installed(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()")
            installed = {name for (name,) in cursor.fetchall()}
        self.assertTrue(GUARD_TRIGGERS <= installed, GUARD_TRIGGERS - installed)
        # Every trigger a MariaDB migration operation creates, and nothing else.
        created = set()
        for migration in MigrationLoader(None, ignore_no_migrations=True).disk_migrations.values():
            for operation in migration.operations:
                if isinstance(operation, MariaDBRunSQL):
                    for statement in operation.sql:
                        created.update(re.findall(r"CREATE TRIGGER (\w+)", statement))
        self.assertEqual(installed, created)
        # 102 existing guards + 3 permission mutexes + 3 immutable-quote guards + 3 PRD-04 evidence guards
        # + 3 CR-01 credit-terms guards + 13 FIN-01 guards (campaign funding evidence, policy reward type,
        # 11 real-reward / deposit / funding-movement guards; the two wallet locks are replaced, not added).
        self.assertEqual(len(installed), 127)

    def test_database_and_connection_settings(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT @@character_set_database, @@collation_database, @@character_set_connection, "
                           "@@tx_isolation, @@time_zone, @@sql_mode")
            charset, collation, connection_charset, isolation, time_zone, sql_mode = cursor.fetchone()
        self.assertEqual((charset, collation, connection_charset), ("utf8mb4", "utf8mb4_unicode_ci", "utf8mb4"))
        self.assertEqual(isolation, "READ-COMMITTED")
        self.assertEqual(time_zone, "+00:00")
        self.assertIn("STRICT_TRANS_TABLES", sql_mode)


@mariadb_only
class IdentityTests(TestCase):
    def setUp(self):
        self.ann = make_user("ann@example.com")
        self.bob = make_user("bob@example.com")

    def test_email_is_unique_ignoring_case(self):
        with refused(self, IntegrityError):
            User.objects.filter(pk=self.bob.pk).update(email="ANN@Example.COM")

    def test_accounts_without_email_coexist(self):
        User.objects.filter(pk=self.ann.pk).update(email=None, phone_number="+255700000001")
        User.objects.filter(pk=self.bob.pk).update(email=None, phone_number="+255700000002")
        self.assertEqual(User.objects.filter(email__isnull=True).count(), 2)

    def test_phone_number_is_unique(self):
        User.objects.filter(pk=self.ann.pk).update(phone_number="+255700000001")
        with refused(self, IntegrityError):
            User.objects.filter(pk=self.bob.pk).update(phone_number="+255700000001")

    def test_accounts_without_phone_coexist(self):
        self.assertEqual(User.objects.filter(phone_number__isnull=True).count(), 2)

    def test_phone_number_format_is_checked(self):
        with refused(self, IntegrityError):
            User.objects.filter(pk=self.bob.pk).update(phone_number="0700000001")


@mariadb_only
class AirtelApprovalTests(ManualPaymentTestCase):
    """One Airtel transaction: one approval, one finalized ledger posting, one activation.
    Through the real approval service, and enforced by the database for writes that bypass it."""

    def cash(self):
        return ledger.get_platform_account("CASH").debit_balance

    def postings(self, claim):
        return JournalEntry.objects.filter(source_type=manual.SOURCE_TYPE, source_id=str(claim.pk))

    def post_for(self, claim):
        """The ledger posting apps.payments.manual.approve() makes, made directly."""
        return ledger.post_entry(
            manual.POSTING_TYPE,
            lines=[ledger.debit(ledger.get_platform_account("CASH"), claim.amount),
                   ledger.credit(ledger.get_platform_account("MEMBERSHIP_DEFERRED_REVENUE"), claim.amount)],
            idempotency_key=f"manual-membership:{claim.pk}", source_type=manual.SOURCE_TYPE,
            source_id=str(claim.pk), external_reference=claim.reference_key, description="test")

    def draft_for(self, claim, credit_amount=None):
        """A DRAFT posting for ``claim``, written in SQL and never finalized."""
        entry_id = uuid.uuid4()
        cash = ledger.get_platform_account("CASH")
        deferred = ledger.get_platform_account("MEMBERSHIP_DEFERRED_REVENUE")
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO wallets_journalentry (id, posting_type, status, description, currency, total_amount,"
                " posted_at, source_type, source_id, external_reference, idempotency_key, fingerprint, request_id)"
                " VALUES (%s, %s, 'DRAFT', 'draft', 'TZS', %s, NOW(6), %s, %s, %s, %s, 'x', '')",
                [entry_id, manual.POSTING_TYPE, claim.amount, manual.SOURCE_TYPE, str(claim.pk),
                 claim.reference_key, f"draft-{entry_id}"])
            for number, (account, side, amount) in enumerate(
                    ((cash, "DEBIT", claim.amount),
                     (deferred, "CREDIT", claim.amount if credit_amount is None else credit_amount)), start=1):
                cursor.execute("INSERT INTO wallets_journalline (id, entry_id, line_number, account_id, side, amount,"
                               " memo) VALUES (%s, %s, %s, %s, %s, %s, '')",
                               [uuid.uuid4(), entry_id, number, account.pk, side, amount])
        return entry_id

    def sql(self, statement, params=()):
        with connection.cursor() as cursor:
            cursor.execute(statement, list(params))

    # --- Through the real approval service ---------------------------------------------------

    def test_approval_posts_once_and_activates_once(self):
        claim = self.submit()
        cash = self.cash()
        approved, changed = self.approve(claim)
        self.assertTrue(changed)
        entry = self.postings(claim).get()
        self.assertEqual(entry.status, "POSTED")
        lines = list(entry.lines.all())
        self.assertEqual(len(lines), 2)
        self.assertEqual(sum(l.amount for l in lines if l.side == "DEBIT"), claim.amount)
        self.assertEqual(sum(l.amount for l in lines if l.side == "CREDIT"), claim.amount)
        self.assertEqual(self.cash(), cash + claim.amount)
        self.assertEqual((approved.status, approved.ledger_entry_id), ("APPROVED", entry.pk))
        subscription = Subscription.objects.get(pk=claim.subscription_id)
        self.assertEqual((subscription.status, subscription.payment_reference),
                         ("ACTIVE", memberships.MANUAL_REFERENCE_PREFIX + claim.reference_key))
        # A retry (double click, repeated request) changes nothing.
        again, changed = self.approve(claim)
        self.assertFalse(changed)
        self.assertEqual(self.postings(claim).count(), 1)
        self.assertEqual(self.cash(), cash + claim.amount)
        self.assertEqual(Subscription.objects.filter(member=self.member, status="ACTIVE").count(), 1)

    def test_a_failure_rolls_back_posting_activation_and_approval(self):
        failures = {
            "after the posting, at activation": "apps.payments.manual.memberships.activate_with_manual_payment",
            "after the approval was written": "apps.payments.manual.record_business_event",
        }
        for label, target in failures.items():
            with self.subTest(label):
                claim = self.submit(member=make_user(f"{uuid.uuid4().hex[:8]}@example.com"))
                cash = self.cash()
                with mock.patch(target, side_effect=RuntimeError("forced failure")), \
                        self.assertRaises(RuntimeError):
                    self.approve(claim)
                claim.refresh_from_db()
                self.assertEqual(claim.status, "PENDING_VERIFICATION")
                self.assertIsNone(claim.ledger_entry_id)
                self.assertFalse(self.postings(claim).exists())
                self.assertEqual(self.cash(), cash)
                self.assertEqual(Subscription.objects.get(pk=claim.subscription_id).status, "PENDING")
                # The claim can still be approved afterwards, once.
                self.assertTrue(self.approve(claim)[1])
                self.assertEqual(self.postings(claim).count(), 1)

    # --- Direct SQL against the ledger -----------------------------------------------------

    def test_draft_posting_moves_no_balance_and_backs_no_approval_or_activation(self):
        claim = self.submit()
        cash = self.cash()
        draft = self.draft_for(claim)
        self.assertEqual(self.cash(), cash)
        with refused(self, IntegrityError):
            ManualMembershipPayment.objects.filter(pk=claim.pk).update(
                status="APPROVED", reviewed_by=self.verifier, reviewed_at=timezone.now(),
                verified_amount=claim.amount, verified_reference_key=claim.reference_key, receipt_confirmed=True,
                ledger_entry_id=draft)
        with refused(self, IntegrityError), transaction.atomic():
            memberships.activate_with_manual_payment(
                Subscription.objects.select_for_update().get(pk=claim.subscription_id),
                reference_key=claim.reference_key, amount=claim.amount, actor=self.verifier,
                submitted_at=timezone.now(), now=timezone.now())
        self.assertEqual(Subscription.objects.get(pk=claim.subscription_id).status, "PENDING")

    def test_unbalanced_posting_cannot_be_finalized(self):
        claim = self.submit()
        cash = self.cash()
        draft = self.draft_for(claim, credit_amount=claim.amount - 1)
        with refused(self, IntegrityError):
            self.sql("UPDATE wallets_journalentry SET status = 'POSTED' WHERE id = %s", [draft])
        self.assertEqual(JournalEntry.objects.get(pk=draft).status, "DRAFT")
        self.assertEqual(self.cash(), cash)

    def test_entries_are_created_as_drafts(self):
        claim = self.submit()
        with refused(self, IntegrityError):
            self.sql("INSERT INTO wallets_journalentry (id, posting_type, status, description, currency, total_amount,"
                     " posted_at, source_type, source_id, external_reference, idempotency_key, fingerprint,"
                     " request_id) VALUES (%s, %s, 'POSTED', 'x', 'TZS', %s, NOW(6), %s, %s, %s, 'k-posted', 'x', '')",
                     [uuid.uuid4(), manual.POSTING_TYPE, claim.amount, manual.SOURCE_TYPE, str(claim.pk),
                      claim.reference_key])

    def test_finalized_posting_cannot_be_edited(self):
        claim = self.submit()
        self.approve(claim)
        entry = self.postings(claim).get()
        cash_account = ledger.get_platform_account("CASH")
        for statement, params in (
            ("UPDATE wallets_journalentry SET total_amount = 1 WHERE id = %s", [entry.pk]),
            ("UPDATE wallets_journalentry SET status = 'DRAFT' WHERE id = %s", [entry.pk]),
            ("UPDATE wallets_journalentry SET external_reference = 'OTHER' WHERE id = %s", [entry.pk]),
            ("DELETE FROM wallets_journalentry WHERE id = %s", [entry.pk]),
            ("UPDATE wallets_journalline SET amount = 1 WHERE entry_id = %s", [entry.pk]),
            ("DELETE FROM wallets_journalline WHERE entry_id = %s", [entry.pk]),
            ("INSERT INTO wallets_journalline (id, entry_id, line_number, account_id, side, amount, memo)"
             " VALUES (%s, %s, 3, %s, 'DEBIT', 1, '')", [uuid.uuid4(), entry.pk, cash_account.pk]),
            ("UPDATE wallets_ledgeraccount SET debit_balance = debit_balance + 1 WHERE id = %s", [cash_account.pk]),
        ):
            with self.subTest(statement=statement), refused(self):
                self.sql(statement, params)
        self.assertEqual(self.cash(), claim.amount)
        self.assertEqual(entry.lines.count(), 2)

    # --- Uniqueness and finality (phase 1), now with real postings -----------------------------

    def test_claims_start_pending(self):
        claim = self.submit()
        with refused(self, IntegrityError):
            clone(claim, reference=new_manual_reference(), status="APPROVED", reference_key=airtel_reference())

    def test_one_approval_per_membership(self):
        claim = self.submit()
        self.approve(claim)
        second = clone(claim, reference=new_manual_reference(), status="PENDING_VERIFICATION",
                       transaction_reference="MPSECOND01", reference_key="MPSECOND01", reviewed_by_id=None,
                       reviewed_at=None, verified_amount=None, verified_reference_key="", receipt_confirmed=False,
                       review_note="", ledger_entry_id=None)
        entry = self.post_for(second)  # Even with its own finalized posting...
        with refused(self, IntegrityError):  # ...a second approval for the membership is refused.
            ManualMembershipPayment.objects.filter(pk=second.pk).update(
                status="APPROVED", reviewed_by=self.verifier, reviewed_at=timezone.now(),
                verified_amount=second.amount, verified_reference_key=second.reference_key, receipt_confirmed=True,
                ledger_entry_id=entry.pk)

    def test_one_open_claim_per_membership(self):
        claim = self.submit()
        with refused(self, IntegrityError):
            clone(claim, reference=new_manual_reference(), reference_key="MPSECOND01")

    def test_transaction_reference_is_single_use(self):
        claim = self.submit()
        other = make_user("other@example.com")
        subscription = self.choose(other)
        with refused(self, IntegrityError):
            clone(claim, reference=new_manual_reference(), member_id=other.pk, subscription_id=subscription.pk)

    def test_rejected_reference_can_be_resubmitted(self):
        claim = self.submit()
        manual.reject(self.verifier, claim, reason="AMOUNT_MISMATCH")
        again = self.submit(subscription=claim.subscription, reference=claim.transaction_reference)
        self.assertEqual(again.status, "PENDING_VERIFICATION")

    def test_approved_claim_is_final(self):
        claim = self.submit()
        self.approve(claim)
        for change in ({"status": "REJECTED"}, {"review_note": "edited"}, {"ledger_entry_id": uuid.uuid4()}):
            with self.subTest(change=change), refused(self):
                ManualMembershipPayment.objects.filter(pk=claim.pk).update(**change)
        with refused(self):
            ManualMembershipPayment.objects.filter(pk=claim.pk).delete()

    def test_submitted_details_cannot_change(self):
        claim = self.submit()
        with refused(self):
            ManualMembershipPayment.objects.filter(pk=claim.pk).update(amount=claim.amount + 1)
        with refused(self):
            # Case-only change: MariaDB's collation would call these equal; the guard does not.
            ManualMembershipPayment.objects.filter(pk=claim.pk).update(plan_name=claim.plan_name.upper())

    def test_membership_activates_once(self):
        claim = self.submit()
        self.approve(claim)
        subscription = Subscription.objects.get(pk=claim.subscription_id)
        with self.assertRaises(memberships.MembershipError), transaction.atomic():
            memberships.activate_with_manual_payment(
                Subscription.objects.select_for_update().get(pk=subscription.pk), reference_key=claim.reference_key,
                amount=claim.amount, actor=self.verifier, submitted_at=timezone.now(), now=timezone.now())
        with refused(self):
            Subscription.objects.filter(pk=subscription.pk).update(payment_reference="MANUAL:OTHER123")
        with refused(self):
            Subscription.objects.filter(pk=subscription.pk).update(amount_paid=1)
        with refused(self, IntegrityError):
            Subscription.objects.filter(pk=subscription.pk).update(status="PENDING")

    def test_payment_reference_activates_one_membership(self):
        claim = self.submit()
        self.approve(claim)
        first = Subscription.objects.get(pk=claim.subscription_id)
        other = self.choose(make_user("other@example.com"))
        with refused(self, IntegrityError):
            Subscription.objects.filter(pk=other.pk).update(
                status="ACTIVE", activated_at=first.activated_at, activation_source=first.activation_source,
                payment_reference=first.payment_reference, amount_paid=first.amount_paid,
                starts_at=first.starts_at, expires_at=first.expires_at)

    def test_one_pending_choice_per_member(self):
        pending = self.choose()
        with refused(self, IntegrityError):
            clone(pending)

    def test_membership_periods_cannot_overlap(self):
        claim = self.submit()
        self.approve(claim)
        active = Subscription.objects.get(pk=claim.subscription_id)
        renewal = clone(active, status="PENDING", activated_at=None, activation_source="", payment_reference="",
                        amount_paid=None, starts_at=None, expires_at=None, activated_by_id=None)
        values = {"status": "ACTIVE", "activated_at": timezone.now(), "activation_source": "PAYMENT",
                  "amount_paid": active.amount_paid}
        with refused(self, IntegrityError):
            Subscription.objects.filter(pk=renewal.pk).update(
                payment_reference="TEST:OVERLAP01", starts_at=active.expires_at - timedelta(days=1),
                expires_at=active.expires_at + timedelta(days=29), **values)
        # Back to back is not an overlap (half-open periods, as tstzrange).
        Subscription.objects.filter(pk=renewal.pk).update(
            payment_reference="TEST:NEXT0001", starts_at=active.expires_at,
            expires_at=active.expires_at + timedelta(days=30), **values)
        self.assertEqual(Subscription.objects.filter(member=self.member, status="ACTIVE").count(), 2)

    def test_subscriptions_cannot_be_deleted(self):
        pending = self.choose()
        with refused(self):
            Subscription.objects.filter(pk=pending.pk).delete()


# --- Audit: append-only, with the authorized purge path ------------------------------------------


def audit_event(days_ago=0):
    event = AuditEvent(category="AUTHENTICATION", action="auth.logout", outcome="SUCCESS", source="SYSTEM",
                       occurred_at=timezone.now() - dt.timedelta(days=days_ago))
    event.save(force_insert=True)
    return event


@mariadb_only
class AuditPurgeTests(TestCase):
    """On MariaDB a delete needs the session flag AND the schema owner (migration) login AND an
    event older than the minimum retention. These tests run as the migration login; the runtime
    login's refusal is in RuntimeLoginTests."""

    def sql(self, statement, params=()):
        with connection.cursor() as cursor:
            cursor.execute(statement, list(params))

    def tearDown(self):
        self.sql("SET @clipcash_audit_purge = NULL")  # Session variables outlive transactions.
        super().tearDown()

    def test_retention_matches_settings(self):
        migration = import_module("apps.audit.migrations.0002_append_only_trigger")
        self.assertEqual(migration.MIN_RETENTION_DAYS, settings.AUDIT_MIN_RETENTION_DAYS)

    def test_updates_are_always_refused(self):
        event = audit_event(800)
        self.sql("SET @clipcash_audit_purge = 'on'")
        with refused(self):
            self.sql("UPDATE audit_auditevent SET outcome = 'FAILURE' WHERE id = %s", [event.pk])

    def test_delete_needs_the_flag(self):
        event = audit_event(800)
        with refused(self):
            self.sql("DELETE FROM audit_auditevent WHERE id = %s", [event.pk])
        self.assertTrue(AuditEvent.objects.filter(pk=event.pk).exists())

    def test_flag_never_deletes_events_within_retention(self):
        recent, expired = audit_event(364), audit_event(366)
        self.sql("SET @clipcash_audit_purge = 'on'")
        with refused(self):
            self.sql("DELETE FROM audit_auditevent WHERE id = %s", [recent.pk])
        self.sql("DELETE FROM audit_auditevent WHERE id = %s", [expired.pk])
        self.assertEqual(set(AuditEvent.objects.filter(pk__in=[recent.pk, expired.pk]).values_list("pk", flat=True)),
                         {recent.pk})

    def test_purge_command_deletes_old_events_and_clears_the_flag(self):
        expired, kept = audit_event(800), audit_event(100)
        cutoff = timezone.now().date() - dt.timedelta(days=400)
        call_command("audit_purge", before=str(cutoff), confirm=True, stdout=StringIO())
        self.assertFalse(AuditEvent.objects.filter(pk=expired.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(pk=kept.pk).exists())
        self.assertEqual(AuditEvent.objects.get(action="audit.purged").metadata["count"], 1)
        with connection.cursor() as cursor:
            cursor.execute("SELECT @clipcash_audit_purge")
            self.assertIsNone(cursor.fetchone()[0])


# --- Login separation ---------------------------------------------------------------------------


class GrantParsingTests(SimpleTestCase):
    """apps.core.mariadb_logins reads SHOW GRANTS output (no database needed)."""

    def test_privileges_on_this_database(self):
        cases = {
            "GRANT USAGE ON *.* TO `rt`@`localhost` IDENTIFIED BY PASSWORD '*AB'": {"USAGE"},
            "GRANT SELECT, INSERT, UPDATE, DELETE ON `clipcash`.* TO `rt`@`localhost`":
                {"SELECT", "INSERT", "UPDATE", "DELETE"},
            "GRANT CREATE TEMPORARY TABLES ON `clip%`.* TO `role`": {"CREATE TEMPORARY TABLES"},
            "GRANT CREATE TEMPORARY TABLES ON `clip\\_cash`.* TO `rt`@`%`": set(),
            "GRANT ALL PRIVILEGES ON `clipcash`.* TO `owner`@`localhost` WITH GRANT OPTION":
                {"ALL PRIVILEGES", "GRANT OPTION"},
            "GRANT SELECT (`a`), UPDATE (`b`, `c`) ON `clipcash`.`t` TO `rt`@`localhost`": {"SELECT", "UPDATE"},
            "GRANT TRIGGER ON *.* TO PUBLIC": {"TRIGGER"},
            "GRANT DROP ON `other`.* TO `rt`@`localhost`": set(),
            "GRANT `some_role` TO `rt`@`localhost`": set(),
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(parse_grant(line, "clipcash"), expected)

    def test_role_grants(self):
        self.assertEqual(parse_role_grants("GRANT `a`, `b` TO `rt`@`localhost`"), ["a", "b"])
        self.assertEqual(parse_role_grants("GRANT SELECT ON *.* TO `rt`@`localhost`"), [])


def with_runtime_login(cls):
    """TransactionTestCase classes that also act as the RUNTIME login (the 'runtime' alias,
    a mirror of the test database: data must be committed for it to see)."""
    if "runtime" in settings.DATABASES:
        cls.databases = {"default", "runtime"}
    return cls


def runtime():
    if "runtime" not in connections:
        raise AssertionError("DB_RUNTIME_USER is not configured: MariaDB tests need the runtime login.")
    return connections["runtime"]


# --- The PostgreSQL concurrency suites, on MariaDB --------------------------------------------
# Their originals are skipped outside PostgreSQL; these subclasses run the same scenarios on
# MariaDB only (and are skipped on PostgreSQL, where the originals run).


def on_mariadb_only(cls):
    cls.__unittest_skip__ = not ON_MARIADB
    cls.__unittest_skip_why__ = "MariaDB port (DB-01) only."
    return cls


@on_mariadb_only
@with_runtime_login
class MariaDBRewardConcurrencyTests(reward_concurrency.RewardConcurrencyTests):
    """Concurrent awards never exceed the campaign's demo budget."""

    def test_budget_cannot_exceed_allocation_in_the_database(self):
        CampaignRewardBudget.objects.get_or_create(campaign=self.campaign)
        with self.assertRaises(IntegrityError), transaction.atomic():
            CampaignRewardBudget.objects.filter(campaign=self.campaign).update(demo_consumed=301)
        CampaignRewardBudget.objects.filter(campaign=self.campaign).update(demo_consumed=300)

    def test_temporary_table_cannot_shadow_the_budget_guard(self):
        """RG-03 on MariaDB, as the RUNTIME login. PostgreSQL closes this by pinning each guard
        function's search_path (core.0004). MariaDB has no search_path: a session's TEMPORARY
        table shadows the table a trigger reads, even when the trigger names it with its schema.
        The runtime login therefore must not hold CREATE TEMPORARY TABLES (by any grant: see
        check_mariadb_logins); then it cannot build the look-alike and the guard reads the
        real campaign."""
        CampaignRewardBudget.objects.get_or_create(campaign=self.campaign)
        key = type(self.campaign)._meta.pk.get_db_prep_value(self.campaign.pk, connection)
        with runtime().cursor() as cursor:
            with self.assertRaises(DatabaseError):
                cursor.execute("CREATE TEMPORARY TABLE advertising_campaign "
                               "(id UUID PRIMARY KEY, demo_budget_amount BIGINT)")
            with self.assertRaises(DatabaseError):
                cursor.execute("INSERT INTO advertising_campaign (id, demo_budget_amount) VALUES (%s, 1)", [key])
            with self.assertRaises(IntegrityError):
                cursor.execute("UPDATE rewards_campaignrewardbudget SET demo_consumed = 301 WHERE campaign_id = %s",
                               [key])
        self.assertEqual(CampaignRewardBudget.objects.get(campaign=self.campaign).demo_consumed, 0)

    def test_reviewed_campaign_budget_cannot_change(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            type(self.campaign).objects.filter(pk=self.campaign.pk).update(demo_budget_amount=10_000)

    def test_session_quote_and_evidence_are_immutable(self):
        session = reward_concurrency.verified_session(self.member("m@x.example"), self.campaign)
        for change in ({"campaign_revision": session.campaign_revision + 1}, {"required_ms": 1},
                       {"video_duration_ms": session.video_duration_ms + 1}, {"credited_ms": 1}):
            with self.subTest(change=change), self.assertRaises(DatabaseError), transaction.atomic():
                WatchSession.objects.filter(pk=session.pk).update(**change)
        with self.assertRaises(DatabaseError), transaction.atomic():
            WatchSession.objects.filter(pk=session.pk).delete()


def db_key(obj):
    return type(obj)._meta.pk.get_db_prep_value(obj.pk, connection)


@mariadb_only
class MariaDBCreditGuardTests(credits.CreditsTestCase):
    """CR-01 guards (rewards.0003/0004, viewing.0004) refuse direct SQL that skips every service.
    The campaign's advertiser-approved budget is 100,000 credits."""

    def execute(self, sql, params):
        with connection.cursor() as cursor:
            cursor.execute(sql, params)

    def test_credit_terms_cannot_be_inserted_above_the_approved_budget(self):
        insert = ("INSERT INTO rewards_campaigncreditterms (campaign_id, credit_budget, version, updated_at) "
                  "VALUES (%s, %s, 1, NOW(6))")
        with self.assertRaisesMessage(IntegrityError, "exceeds the advertiser-approved"), transaction.atomic():
            self.execute(insert, [db_key(self.campaign), 100_001])
        self.assertFalse(credits.CampaignCreditTerms.objects.filter(campaign=self.campaign).exists())
        self.execute(insert, [db_key(self.campaign), 100_000])  # Equal to the approved budget: allowed.

    def test_credit_terms_cannot_be_raised_above_approved_or_lowered_below_awarded(self):
        self.approved_policy(amount=100)
        award = credits.services.evaluate_session(self.verified_session().pk)
        self.assertEqual(award.amount, 100)
        self.terms(budget=500)
        update = "UPDATE rewards_campaigncreditterms SET credit_budget = %s WHERE campaign_id = %s"
        for value, message in ((100_001, "exceeds the advertiser-approved"), (99, "below the credits already awarded")):
            with self.subTest(value=value), self.assertRaisesMessage(IntegrityError, message), transaction.atomic():
                self.execute(update, [value, db_key(self.campaign)])
        self.assertEqual(credits.CampaignCreditTerms.objects.get(campaign=self.campaign).credit_budget, 500)
        self.execute(update, [100, db_key(self.campaign)])  # Equal to the awarded credits: allowed.

    def test_consumption_is_capped_at_the_staff_credit_budget(self):
        """rewards.0003: with terms, the cap is the staff budget (150), not the campaign's 100,000."""
        self.terms(budget=150)
        CampaignRewardBudget.objects.get_or_create(campaign=self.campaign)
        update = "UPDATE rewards_campaignrewardbudget SET demo_consumed = %s WHERE campaign_id = %s"
        with self.assertRaisesMessage(IntegrityError, "exceeds the campaign demo budget"), transaction.atomic():
            self.execute(update, [151, db_key(self.campaign)])
        self.execute(update, [150, db_key(self.campaign)])
        self.assertEqual(CampaignRewardBudget.objects.get(campaign=self.campaign).demo_consumed, 150)

    def test_consumption_without_terms_is_capped_at_the_approved_budget(self):
        CampaignRewardBudget.objects.get_or_create(campaign=self.campaign)
        update = "UPDATE rewards_campaignrewardbudget SET demo_consumed = %s WHERE campaign_id = %s"
        with self.assertRaisesMessage(IntegrityError, "exceeds the campaign demo budget"), transaction.atomic():
            self.execute(update, [100_001, db_key(self.campaign)])
        self.execute(update, [100_000, db_key(self.campaign)])

    def test_revision_cannot_be_approved_below_active_credit_terms(self):
        from apps.advertising import services as advertising

        self.terms(budget=80_000)
        campaign = advertising.reopen_campaign(self.owner, advertising.pause_campaign(self.owner, self.campaign))
        campaign = advertising.update_campaign(self.owner, campaign, {"demo_budget_amount": 50_000},
                                               expected_revision=campaign.revision)
        campaign = advertising.submit_campaign(self.owner, campaign)
        approve = ("UPDATE advertising_campaign SET status = 'SCHEDULED', approved_revision = revision, "
                   "reviewed_at = NOW(6), reviewed_by_id = %s WHERE id = %s")
        with self.assertRaisesMessage(IntegrityError, "below its active credit budget"), transaction.atomic():
            self.execute(approve, [db_key(self.root), db_key(campaign)])
        campaign.refresh_from_db()
        self.assertNotEqual(campaign.approved_revision, campaign.revision)

    def test_quoted_offer_cannot_change(self):
        self.approved_policy(amount=100)
        session = self.start()
        self.assertEqual(session.quoted_credits, 100)
        for column, value in (("quoted_credits", 999), ("quoted_credits", None), ("quoted_policy_id", None)):
            with self.subTest(column=column, value=value), \
                    self.assertRaisesMessage(DatabaseError, "watch session identity cannot change"), \
                    transaction.atomic():
                self.execute(f"UPDATE viewing_watchsession SET {column} = %s WHERE id = %s", [value, db_key(session)])
        session.refresh_from_db()
        self.assertEqual(session.quoted_credits, 100)

    def test_session_without_a_quote_cannot_gain_one(self):
        """Sessions from before viewing.0004 keep NULL: a quote cannot be added afterwards."""
        policy = self.approved_policy(amount=100)
        session = credits.quoted_verified_session(self.member, self.campaign, None, None)
        for column, value in (("quoted_credits", 100), ("quoted_policy_id", db_key(policy))):
            with self.subTest(column=column), \
                    self.assertRaisesMessage(DatabaseError, "watch session identity cannot change"), \
                    transaction.atomic():
                self.execute(f"UPDATE viewing_watchsession SET {column} = %s WHERE id = %s", [value, db_key(session)])


@on_mariadb_only
class MariaDBCreditTermsMigrationTests(credit_migration.CreditTermsMigrationTests):
    """The CR-01 MariaDB triggers, reversed to before rewards.0003 and re-applied over an
    existing award (the PostgreSQL original is skipped here)."""


@on_mariadb_only
class MariaDBMembershipConcurrencyTests(membership_concurrency.MembershipConcurrencyTests):
    pass


@on_mariadb_only
class MariaDBManualPaymentConcurrencyTests(manual_concurrency.ManualPaymentConcurrencyTests):
    """Concurrent approvals (one posting, one activation, CASH = the fee once), approve
    against reject, and concurrent submissions, on MariaDB connections."""

    def test_retries_after_concurrent_approval_change_nothing(self):
        claim = self.submit(self.member, "MP60606060")
        results = manual_concurrency.run_threads(3, lambda i: manual.approve(
            self.verifiers[i % 2], claim, verified_amount=5_000, verified_reference="MP60606060",
            receipt_confirmed=True, recipient_confirmed=True))
        self.assertTrue(all(isinstance(result, tuple) for result in results), results)
        for _ in range(2):
            self.assertFalse(manual.approve(self.verifiers[0], claim, verified_amount=5_000,
                                            verified_reference="MP60606060", receipt_confirmed=True,
                                            recipient_confirmed=True)[1])
        self.assertEqual(JournalEntry.objects.count(), 1)
        self.assertEqual(ledger.get_platform_account("CASH").balance, 5_000)
        self.assertEqual(Subscription.objects.filter(member=self.member, status="ACTIVE").count(), 1)


@on_mariadb_only
@with_runtime_login
class RuntimeLoginTests(manual_concurrency.ManualPaymentConcurrencyTests):
    """The application's own login (DB_RUNTIME_USER) against the finalized ledger and the guards."""

    # This class reuses the fixture only.
    test_concurrent_approvals_activate_once = None
    test_concurrent_approve_and_reject_decide_once = None
    test_same_reference_from_many_members_is_accepted_once = None
    test_double_submission_by_one_member_creates_one_claim = None

    def test_login_separation_checks_pass(self):
        self.assertEqual(runtime_login_findings(runtime()), [])
        self.assertEqual(migration_login_findings(connection, runtime()), [])

    def test_runtime_login_cannot_alter_the_guards(self):
        # Never try these with an unsafe login: they would really drop the guards for later tests.
        self.assertEqual(runtime_login_findings(runtime()), [], "unsafe runtime login; destructive probes skipped")
        with runtime().cursor() as cursor:
            for statement in ("CREATE TEMPORARY TABLE wallets_journalline (id INT)",
                              "DROP TRIGGER ledger_entry_finalize_check",
                              "CREATE TRIGGER clipcash_probe BEFORE INSERT ON core_job FOR EACH ROW SET @x = 1",
                              "ALTER TABLE wallets_journalentry DROP CONSTRAINT ledger_entry_status",
                              "TRUNCATE TABLE wallets_journalline"):
                with self.subTest(statement=statement), self.assertRaises(DatabaseError):
                    cursor.execute(statement)

    def test_runtime_login_cannot_tamper_with_the_ledger(self):
        claim = self.submit(self.member, "MP50505050")
        manual.approve(self.verifiers[0], claim, verified_amount=5_000, verified_reference="MP50505050",
                       receipt_confirmed=True, recipient_confirmed=True)
        entry = JournalEntry.objects.get()
        cash = ledger.get_platform_account("CASH")
        deferred = ledger.get_platform_account("MEMBERSHIP_DEFERRED_REVENUE")
        draft = uuid.uuid4()
        with runtime().cursor() as cursor:
            for statement, params in (
                ("UPDATE wallets_ledgeraccount SET debit_balance = 999999 WHERE id = %s", [cash.pk]),
                ("UPDATE wallets_journalentry SET total_amount = 1 WHERE id = %s", [entry.pk]),
                ("UPDATE wallets_journalentry SET status = 'DRAFT' WHERE id = %s", [entry.pk]),
                ("DELETE FROM wallets_journalline WHERE entry_id = %s", [entry.pk]),
                ("UPDATE wallets_journalline SET amount = 1 WHERE entry_id = %s", [entry.pk]),
                ("UPDATE payments_manualmembershippayment SET ledger_entry_id = %s WHERE id = %s",
                 [uuid.uuid4(), claim.pk]),
            ):
                with self.subTest(statement=statement), self.assertRaises(DatabaseError):
                    cursor.execute(statement, params)
            # The runtime login may write drafts (the service does), but an unbalanced one never posts.
            cursor.execute(
                "INSERT INTO wallets_journalentry (id, posting_type, status, description, currency, total_amount,"
                " posted_at, source_type, source_id, external_reference, idempotency_key, fingerprint, request_id)"
                " VALUES (%s, 'test.raw', 'DRAFT', 'x', 'TZS', 100, NOW(6), 'raw', '', '', %s, 'x', '')",
                [draft, f"raw-{draft}"])
            cursor.execute("INSERT INTO wallets_journalline (id, entry_id, line_number, account_id, side, amount, memo)"
                           " VALUES (%s, %s, 1, %s, 'DEBIT', 100, '')", [uuid.uuid4(), draft, cash.pk])
            cursor.execute("INSERT INTO wallets_journalline (id, entry_id, line_number, account_id, side, amount, memo)"
                           " VALUES (%s, %s, 2, %s, 'CREDIT', 99, '')", [uuid.uuid4(), draft, deferred.pk])
            with self.assertRaises(IntegrityError):
                cursor.execute("UPDATE wallets_journalentry SET status = 'POSTED' WHERE id = %s", [draft])
        self.assertEqual(ledger.get_platform_account("CASH").balance, 5_000)
        self.assertEqual(JournalEntry.objects.get(pk=draft).status, "DRAFT")
        self.assertEqual(JournalEntry.objects.get(pk=entry.pk).total_amount, 5_000)

    def test_concurrent_provisioning_and_retries_as_runtime(self):
        from django.contrib.auth.models import Group
        from apps.accounts.roles import ROLE_GROUP_NAMES, ROLE_PERMISSIONS

        users = [make_user(f"runtime-provision{i}@example.com", role=User.Role.CONTENT_MANAGER)
                 for i in range(3)]
        for user in users:
            user.groups.clear()

        def provision(_):
            call_command("provision_staff_permissions", apply=True, stdout=StringIO())
            return "applied"

        self.assertEqual(RuntimeWorkersMixin.runtime_threads(4, provision), ["applied"] * 4)
        self.assertEqual(RuntimeWorkersMixin.runtime_threads(2, provision), ["applied"] * 2)
        for role, name in ROLE_GROUP_NAMES.items():
            group = Group.objects.get(name=name)
            held = {f"{p.content_type.app_label}.{p.codename}"
                    for p in group.permissions.select_related("content_type")}
            self.assertEqual(held, set(ROLE_PERMISSIONS[role]))
            self.assertEqual(group.permissions.through.objects.filter(group=group).count(), len(held))
        for user in users:
            self.assertEqual(list(user.groups.values_list("name", flat=True)),
                             [ROLE_GROUP_NAMES["CONTENT_MANAGER"]])

    def test_runtime_purge_command_is_refused_and_clears_flag(self):
        expired = audit_event(800)

        def purge(_):
            cutoff = timezone.now().date() - timedelta(days=400)
            with self.assertRaises(DatabaseError):
                call_command("audit_purge", before=str(cutoff), confirm=True, stdout=StringIO())
            with connection.cursor() as cursor:
                cursor.execute("SELECT @clipcash_audit_purge")
                self.assertIsNone(cursor.fetchone()[0])
            return "refused"

        self.assertEqual(RuntimeWorkersMixin.runtime_threads(1, purge), ["refused"])
        self.assertTrue(AuditEvent.objects.filter(pk=expired.pk).exists())
        self.assertFalse(AuditEvent.objects.filter(action="audit.purged").exists())

    def test_runtime_login_cannot_purge_audit_events(self):
        expired = audit_event(800)
        with runtime().cursor() as cursor:
            try:
                cursor.execute("SET @clipcash_audit_purge = 'on'")  # Any session can set the flag...
                for statement in ("DELETE FROM audit_auditevent WHERE id = %s",  # ...but only the owner purges.
                                  "UPDATE audit_auditevent SET outcome = 'FAILURE' WHERE id = %s"):
                    with self.subTest(statement=statement), self.assertRaises(DatabaseError):
                        cursor.execute(statement, [expired.pk])
                with self.assertRaises(DatabaseError):
                    cursor.execute("TRUNCATE TABLE audit_auditevent")
            finally:
                cursor.execute("SET @clipcash_audit_purge = NULL")
        self.assertTrue(AuditEvent.objects.filter(pk=expired.pk, outcome="SUCCESS").exists())


@on_mariadb_only
class MariaDBComplimentaryConcurrencyTests(complimentary_concurrency.ComplimentaryConcurrencyTests):
    """Retry keys and member locks prevent duplicate and overlapping free grants."""
    pass


class RuntimeWorkersMixin:
    """Fixtures/flush use the owner; worker services use separate restricted connections."""

    worker_modules = ()

    def setUp(self):
        super().setUp()
        for module in self.worker_modules:
            patch = mock.patch.object(module, "run_threads", self.runtime_threads)
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def runtime_threads(count, target):
        def worker(index):
            owner = connections["default"]
            restricted = owner.copy(alias="default")
            restricted.settings_dict.update(
                USER=settings.DATABASES["runtime"]["USER"],
                PASSWORD=settings.DATABASES["runtime"]["PASSWORD"])
            connections["default"] = restricted
            try:
                with restricted.cursor() as cursor:
                    cursor.execute("SELECT CURRENT_USER()")
                    assert cursor.fetchone()[0].split("@")[0] == settings.DATABASES["runtime"]["USER"]
                return target(index)
            finally:
                restricted.close()
                connections["default"] = owner
        return run_threads(count, worker)


@on_mariadb_only
class RuntimeComplimentaryConcurrencyTests(RuntimeWorkersMixin, complimentary_concurrency.ComplimentaryConcurrencyTests):
    worker_modules = (complimentary_concurrency,)


@on_mariadb_only
class RuntimeManualConcurrencyTests(RuntimeWorkersMixin, MariaDBManualPaymentConcurrencyTests):
    worker_modules = (manual_concurrency,)


@on_mariadb_only
class TransactionLockTests(TransactionTestCase):
    def test_lock_ends_at_commit_and_rollback_with_session_still_open(self):
        import threading
        from apps.core.locking import transaction_lock_mariadb

        for rollback in (False, True):
            with self.subTest(rollback=rollback):
                attempted, acquired = threading.Event(), threading.Event()
                errors = []

                def wait_for_lock():
                    try:
                        with transaction.atomic():
                            attempted.set()
                            transaction_lock_mariadb(f"test-release-{rollback}")
                            acquired.set()
                    except Exception as error:
                        errors.append(error)
                    finally:
                        connections.close_all()

                with transaction.atomic():
                    transaction_lock_mariadb(f"test-release-{rollback}")
                    thread = threading.Thread(target=wait_for_lock)
                    thread.start()
                    self.assertTrue(attempted.wait(5))
                    self.assertFalse(acquired.wait(0.2))
                    transaction.set_rollback(rollback)
                # Main session is intentionally kept open, unlike a session-scoped lock.
                self.assertTrue(acquired.wait(5))
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])


@mariadb_only
class MariaDBHealthTests(TestCase):
    def test_owner_health_reports_attention_and_readiness_reads_guards(self):
        from apps.administration import summary
        from apps.administration.management.commands.demo_readiness import readiness
        from apps.core.mariadb_guards import guard_findings
        self.assertEqual(guard_findings(connection), [])
        restricted = connection.settings_dict["USER"] == settings.DATABASES["runtime"]["USER"]
        self.assertEqual(bool(summary.database_role_findings()), not restricted)
        root = make_user("health@example.com", role=User.Role.SUPER_ADMIN)
        checks = {check.key: check for check in summary.health(root)}
        self.assertEqual(checks["database_roles"].status, summary.HEALTHY if restricted else summary.ATTENTION)
        locks = [result for result in readiness() if result[1] == "Financial locks"]
        self.assertEqual(locks[0][0], "PASS")
