import importlib
import uuid

from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.wallets import chart, services
from apps.wallets.exceptions import PostingValidationError
from apps.wallets.models import JournalEntry, LedgerAccount

MIGRATION_0002 = importlib.import_module("apps.wallets.migrations.0002_ledger_integrity")
MIGRATION_0003 = importlib.import_module("apps.wallets.migrations.0003_platform_accounts")


class ChartOfAccountsTests(TestCase):
    def test_platform_accounts_created_with_zero_balances(self):
        accounts = LedgerAccount.objects.filter(scope_type=chart.Scope.PLATFORM)
        self.assertEqual({a.account_type for a in accounts}, {t.code for t in chart.PLATFORM_ACCOUNT_TYPES})
        for account in accounts:
            definition = chart.ACCOUNT_TYPES[account.account_type]
            with self.subTest(account=account.code):
                self.assertEqual(account.code, chart.account_code(account.account_type))
                self.assertEqual((account.classification, account.funding_class, account.overdraft_protected),
                                 (definition.classification, definition.funding_class, definition.overdraft_protected))
                self.assertEqual((account.debit_balance, account.currency, account.status), (0, "TZS", "ACTIVE"))

    def test_no_transactions_or_member_balances_seeded(self):
        self.assertFalse(JournalEntry.objects.exists())
        self.assertFalse(LedgerAccount.objects.exclude(scope_type=chart.Scope.PLATFORM).exists())

    def test_frozen_migration_definitions_match_chart(self):
        for type_code, name, classification, funding_class, protected in MIGRATION_0003.PLATFORM_ACCOUNTS:
            definition = chart.ACCOUNT_TYPES[type_code]
            self.assertEqual((name, classification, funding_class, protected),
                             (definition.name, definition.classification, definition.funding_class,
                              definition.overdraft_protected))

    def test_frozen_database_rules_match_chart(self):
        sql = MIGRATION_0002.FORWARD
        for credited, sources in chart.PROTECTED_INFLOWS.items():
            self.assertIn(f"a.funding_class = '{credited}')", sql)
            self.assertIn("NOT IN (" + ", ".join(f"'{s}'" for s in sources) + ")", sql)
        self.assertIn("ARRAY[" + ", ".join(f"'{c}'" for c in chart.SEGREGATED_FROM_MEMBERSHIP) + "]", sql)

    def test_every_account_type_classification_is_consistent(self):
        for definition in chart.ACCOUNT_TYPES.values():
            self.assertIn(definition.classification, dict(chart.Classification.choices))
            self.assertIn(definition.funding_class, dict(chart.FundingClass.choices))
            if definition.funding_class == chart.FundingClass.REWARD_RESTRICTED:
                self.assertTrue(definition.overdraft_protected, definition.code)


class AccountConstraintTests(TestCase):
    def create(self, **overrides):
        values = {"code": f"x:{uuid.uuid4()}", "name": "x", "account_type": "CAMPAIGN_RESTRICTED_FUNDING",
                  "classification": "LIABILITY", "funding_class": "REWARD_RESTRICTED", "scope_type": "CAMPAIGN",
                  "scope_id": str(uuid.uuid4()), "overdraft_protected": True}
        values.update(overrides)
        return LedgerAccount.objects.create(**values)

    def test_type_attribute_mismatches_rejected(self):
        for overrides in ({"classification": "ASSET"}, {"funding_class": "MEMBERSHIP"},
                          {"scope_type": "PLATFORM", "scope_id": ""}, {"overdraft_protected": False},
                          {"currency": "USD"}, {"scope_id": ""}):
            with self.subTest(overrides=overrides), self.assertRaises(IntegrityError), transaction.atomic():
                self.create(**overrides)

    def test_new_accounts_always_start_at_zero(self):
        account = self.create(debit_balance=-500_000)  # Attempt to create funds out of thin air.
        account.refresh_from_db()
        self.assertEqual(account.debit_balance, 0)


class OpenAccountTests(TestCase):
    def test_open_scoped_account_once_and_audit(self):
        from apps.audit.models import AuditEvent

        scope = str(uuid.uuid4())
        first = services.open_account("CAMPAIGN_RESTRICTED_FUNDING", scope_id=scope)
        second = services.open_account("CAMPAIGN_RESTRICTED_FUNDING", scope_id=scope)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.code, f"campaign_restricted_funding:{scope}")
        self.assertEqual(AuditEvent.objects.filter(action="ledger.account.opened").count(), 1)

    def test_invalid_requests(self):
        for account_type, scope_id in (("CASH", "x"), ("NOPE", "x"), ("REWARD_LIABILITY", ""),
                                       ("REWARD_LIABILITY", "x" * 65)):
            with self.subTest(account_type=account_type), self.assertRaises(PostingValidationError):
                services.open_account(account_type, scope_id=scope_id)
