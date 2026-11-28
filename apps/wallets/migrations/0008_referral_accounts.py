"""
REF-01: referral programme accounts and a narrow extension of the wallet lock.

* Chart: three platform accounts for ClipCash's own marketing budget (pending verification,
  verified, committed to the referral programme). They reuse the advertiser-verified and
  reward-restricted funding classes, so the ledger's database funding-separation rules (0002) apply
  unchanged: membership money can never fund them and rewards only come from verified, committed
  money. Accounts only: no entries or balances are created.
* PENDING wallet accounts additionally accept referral.reward_pending, referral.reward_cleared and
  referral.pending_reversed; AVAILABLE accounts referral.reward_cleared and referral.cleared_reversed.
  RESERVED (withdrawal) accounts are unchanged (withdrawal postings only). WalletTransaction rows stay
  insert-only mirrors of an allowed posting on the wallet's own account.
* Ledger balance, overdraft, funding-separation and immutability rules are unchanged.

Rollback restores wallets 0007 exactly (referral postings refused again); posted entries and the
accounts remain (financial records are never deleted).
"""

from importlib import import_module

from django.db import migrations, models

from apps.core.migration_ops import MariaDBRunSQL, PostgreSQLRunSQL, check_error, privilege_error, trigger

PREVIOUS = import_module("apps.wallets.migrations.0007_withdrawal_postings")
SEARCH_PATH = "pg_catalog, public, pg_temp"
REWARDS = PREVIOUS.REWARDS
AVAILABLE_WITHDRAWALS = PREVIOUS.AVAILABLE_WITHDRAWALS
RESERVED_TYPES = PREVIOUS.RESERVED_TYPES
REFERRAL_PENDING = "'referral.reward_pending', 'referral.reward_cleared', 'referral.pending_reversed'"
REFERRAL_AVAILABLE = "'referral.reward_cleared', 'referral.cleared_reversed'"
REFERRAL_ALL = ("'referral.reward_pending', 'referral.reward_cleared', 'referral.pending_reversed', "
                "'referral.cleared_reversed'")

# Frozen copy of the chart entries (later chart edits must not change history).
PLATFORM_ACCOUNTS = [
    # (type, name, classification, funding_class)
    ("MARKETING_FUNDING_PENDING", "ClipCash marketing funds received, pending verification", "LIABILITY",
     "ADVERTISER_UNVERIFIED"),
    ("MARKETING_FUNDING_VERIFIED", "ClipCash marketing budget verified, unallocated", "LIABILITY",
     "ADVERTISER_VERIFIED"),
    ("REFERRAL_PROGRAMME_FUNDING", "Referral programme restricted funding", "LIABILITY", "REWARD_RESTRICTED"),
]


def create_accounts(apps, schema_editor):
    LedgerAccount = apps.get_model("wallets", "LedgerAccount")
    for account_type, name, classification, funding_class in PLATFORM_ACCOUNTS:
        LedgerAccount.objects.get_or_create(
            account_type=account_type, scope_type="PLATFORM", scope_id="",
            defaults={"code": account_type.lower(), "name": name, "classification": classification,
                      "funding_class": funding_class, "overdraft_protected": True, "currency": "TZS",
                      "status": "ACTIVE"})


FORWARD = rf"""
CREATE OR REPLACE FUNCTION wallets_block_member_postings() RETURNS trigger
LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$
DECLARE kind text;
BEGIN
    SELECT e.posting_type INTO kind FROM wallets_journalentry e WHERE e.id = NEW.entry_id;
    IF EXISTS (SELECT 1 FROM wallets_wallet w WHERE w.reserved_account_id = NEW.account_id)
       AND kind NOT IN ({RESERVED_TYPES}) THEN
        RAISE EXCEPTION 'withdrawal reservations accept only withdrawal postings'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF EXISTS (SELECT 1 FROM wallets_wallet w WHERE w.pending_account_id = NEW.account_id)
       AND kind NOT IN ({REWARDS}, {REFERRAL_PENDING}) THEN
        RAISE EXCEPTION 'member wallet postings are limited to reward postings'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF EXISTS (SELECT 1 FROM wallets_wallet w WHERE w.available_account_id = NEW.account_id)
       AND kind NOT IN ({REWARDS}, {AVAILABLE_WITHDRAWALS}, {REFERRAL_AVAILABLE}) THEN
        RAISE EXCEPTION 'member wallet postings are limited to reward and withdrawal postings'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION wallets_transactions_locked() RETURNS trigger
LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'wallet transactions are immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM wallets_journalline l
        JOIN wallets_journalentry e ON e.id = l.entry_id
        JOIN wallets_wallet w ON w.id = NEW.wallet_id
        WHERE l.entry_id = NEW.journal_entry_id AND l.amount = NEW.amount
          AND e.posting_type IN ({REWARDS}, {RESERVED_TYPES}, {REFERRAL_ALL})
          AND ((NEW.bucket = 'AVAILABLE' AND l.account_id = w.available_account_id)
               OR (NEW.bucket = 'PENDING' AND l.account_id = w.pending_account_id)
               OR (NEW.bucket = 'RESERVED' AND l.account_id = w.reserved_account_id))
          AND ((NEW.direction = 'IN' AND l.side = 'CREDIT') OR (NEW.direction = 'OUT' AND l.side = 'DEBIT'))
    ) THEN
        RAISE EXCEPTION 'a wallet transaction must mirror an allowed posting on its own wallet'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
"""
REVERSE = PREVIOUS.FORWARD

MARIADB_FORWARD = [
    "DROP TRIGGER IF EXISTS wallets_block_member_postings",
    f"""CREATE TRIGGER wallets_block_member_postings BEFORE INSERT ON wallets_journalline FOR EACH ROW
FOLLOWS ledger_line_insert
BEGIN
    DECLARE kind VARCHAR(64);
    SELECT e.posting_type INTO kind FROM wallets_journalentry e WHERE e.id = NEW.entry_id;
    IF EXISTS (SELECT 1 FROM wallets_wallet w WHERE w.reserved_account_id = NEW.account_id)
       AND NOT (BINARY kind IN ({RESERVED_TYPES})) THEN
        {privilege_error("withdrawal reservations accept only withdrawal postings")}
    END IF;
    IF EXISTS (SELECT 1 FROM wallets_wallet w WHERE w.pending_account_id = NEW.account_id)
       AND NOT (BINARY kind IN ({REWARDS}, {REFERRAL_PENDING})) THEN
        {privilege_error("member wallet postings are limited to reward postings")}
    END IF;
    IF EXISTS (SELECT 1 FROM wallets_wallet w WHERE w.available_account_id = NEW.account_id)
       AND NOT (BINARY kind IN ({REWARDS}, {AVAILABLE_WITHDRAWALS}, {REFERRAL_AVAILABLE})) THEN
        {privilege_error("member wallet postings are limited to reward and withdrawal postings")}
    END IF;
END""",
    "DROP TRIGGER IF EXISTS wallets_transactions_locked_insert",
    trigger("wallets_transactions_locked_insert", "BEFORE INSERT", "wallets_wallettransaction", f"""
    IF NOT EXISTS (
        SELECT 1 FROM wallets_journalline l
        JOIN wallets_journalentry e ON e.id = l.entry_id
        JOIN wallets_wallet w ON w.id = NEW.wallet_id
        WHERE l.entry_id = NEW.journal_entry_id AND l.amount = NEW.amount
          AND BINARY e.posting_type IN ({REWARDS}, {RESERVED_TYPES}, {REFERRAL_ALL})
          AND ((BINARY NEW.bucket = 'AVAILABLE' AND l.account_id = w.available_account_id)
               OR (BINARY NEW.bucket = 'PENDING' AND l.account_id = w.pending_account_id)
               OR (BINARY NEW.bucket = 'RESERVED' AND l.account_id = w.reserved_account_id))
          AND ((BINARY NEW.direction = 'IN' AND BINARY l.side = 'CREDIT')
               OR (BINARY NEW.direction = 'OUT' AND BINARY l.side = 'DEBIT'))
    ) THEN
        {check_error("a wallet transaction must mirror an allowed posting on its own wallet")}
    END IF;"""),
]
MARIADB_REVERSE = PREVIOUS.MARIADB_FORWARD


class Migration(migrations.Migration):

    dependencies = [
        ('wallets', '0007_withdrawal_postings'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='ledgeraccount',
            name='ledger_account_type_classification',
        ),
        migrations.RemoveConstraint(
            model_name='ledgeraccount',
            name='ledger_account_type_funding_class',
        ),
        migrations.RemoveConstraint(
            model_name='ledgeraccount',
            name='ledger_account_type_scope',
        ),
        migrations.RemoveConstraint(
            model_name='ledgeraccount',
            name='ledger_account_type_protection',
        ),
        migrations.AlterField(
            model_name='ledgeraccount',
            name='account_type',
            field=models.CharField(choices=[('CASH', 'Cash at bank and mobile-money float'), ('PAYMENT_CLEARING', 'Payment provider clearing'), ('ADVERTISER_RECEIVABLE', 'Advertiser receivables'), ('MEMBERSHIP_DEFERRED_REVENUE', 'Membership fees received in advance'), ('ADVERTISER_FUNDING_PENDING', 'Advertiser funds received, pending verification'), ('ADVERTISER_FUNDING_VERIFIED', 'Advertiser funds verified, unallocated'), ('MARKETING_FUNDING_PENDING', 'ClipCash marketing funds received, pending verification'), ('MARKETING_FUNDING_VERIFIED', 'ClipCash marketing budget verified, unallocated'), ('CAMPAIGN_RESTRICTED_FUNDING', 'Campaign restricted reward funding'), ('REFERRAL_PROGRAMME_FUNDING', 'Referral programme restricted funding'), ('REWARD_LIABILITY', 'Rewards owed to members'), ('MEMBER_REWARD_PENDING', 'Member rewards pending clearance'), ('MEMBER_WITHDRAWAL_RESERVED', 'Member withdrawal reservations'), ('WITHDRAWAL_CLEARING', 'Withdrawals in progress'), ('REFUNDS_PAYABLE', 'Refunds payable'), ('OWNER_EQUITY', 'Owner equity'), ('MEMBERSHIP_REVENUE', 'Membership revenue'), ('ADVERTISING_SERVICE_REVENUE', 'Advertising service revenue'), ('OTHER_OPERATING_REVENUE', 'Other operating revenue'), ('PAYMENT_PROCESSING_FEES', 'Payment processing fees'), ('OPERATING_EXPENSES', 'Operating expenses')], max_length=40, verbose_name='account type'),
        ),
        migrations.AlterField(
            model_name='wallettransaction',
            name='category',
            field=models.CharField(choices=[('REWARD_CREDIT', 'Reward'), ('REWARD_CLEARANCE', 'Reward cleared'), ('REFERRAL_CREDIT', 'Referral reward'), ('REFERRAL_CLEARANCE', 'Referral reward cleared'), ('REFERRAL_REVERSAL', 'Referral reward reversed'), ('WITHDRAWAL_RESERVATION', 'Withdrawal reserved'), ('WITHDRAWAL_SETTLEMENT', 'Withdrawal paid'), ('WITHDRAWAL_RELEASE', 'Withdrawal released'), ('CORRECTION', 'Authorized correction'), ('REVERSAL', 'Reversal')], max_length=24, verbose_name='category'),
        ),
        migrations.AddConstraint(
            model_name='ledgeraccount',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('account_type', 'CASH'), ('classification', 'ASSET')), models.Q(('account_type', 'PAYMENT_CLEARING'), ('classification', 'ASSET')), models.Q(('account_type', 'ADVERTISER_RECEIVABLE'), ('classification', 'ASSET')), models.Q(('account_type', 'MEMBERSHIP_DEFERRED_REVENUE'), ('classification', 'LIABILITY')), models.Q(('account_type', 'ADVERTISER_FUNDING_PENDING'), ('classification', 'LIABILITY')), models.Q(('account_type', 'ADVERTISER_FUNDING_VERIFIED'), ('classification', 'LIABILITY')), models.Q(('account_type', 'MARKETING_FUNDING_PENDING'), ('classification', 'LIABILITY')), models.Q(('account_type', 'MARKETING_FUNDING_VERIFIED'), ('classification', 'LIABILITY')), models.Q(('account_type', 'CAMPAIGN_RESTRICTED_FUNDING'), ('classification', 'LIABILITY')), models.Q(('account_type', 'REFERRAL_PROGRAMME_FUNDING'), ('classification', 'LIABILITY')), models.Q(('account_type', 'REWARD_LIABILITY'), ('classification', 'LIABILITY')), models.Q(('account_type', 'MEMBER_REWARD_PENDING'), ('classification', 'LIABILITY')), models.Q(('account_type', 'MEMBER_WITHDRAWAL_RESERVED'), ('classification', 'LIABILITY')), models.Q(('account_type', 'WITHDRAWAL_CLEARING'), ('classification', 'LIABILITY')), models.Q(('account_type', 'REFUNDS_PAYABLE'), ('classification', 'LIABILITY')), models.Q(('account_type', 'OWNER_EQUITY'), ('classification', 'EQUITY')), models.Q(('account_type', 'MEMBERSHIP_REVENUE'), ('classification', 'REVENUE')), models.Q(('account_type', 'ADVERTISING_SERVICE_REVENUE'), ('classification', 'REVENUE')), models.Q(('account_type', 'OTHER_OPERATING_REVENUE'), ('classification', 'REVENUE')), models.Q(('account_type', 'PAYMENT_PROCESSING_FEES'), ('classification', 'EXPENSE')), models.Q(('account_type', 'OPERATING_EXPENSES'), ('classification', 'EXPENSE')), _connector='OR'), name='ledger_account_type_classification'),
        ),
        migrations.AddConstraint(
            model_name='ledgeraccount',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('account_type', 'CASH'), ('funding_class', 'TREASURY')), models.Q(('account_type', 'PAYMENT_CLEARING'), ('funding_class', 'TREASURY')), models.Q(('account_type', 'ADVERTISER_RECEIVABLE'), ('funding_class', 'ADVERTISER_UNVERIFIED')), models.Q(('account_type', 'MEMBERSHIP_DEFERRED_REVENUE'), ('funding_class', 'MEMBERSHIP')), models.Q(('account_type', 'ADVERTISER_FUNDING_PENDING'), ('funding_class', 'ADVERTISER_UNVERIFIED')), models.Q(('account_type', 'ADVERTISER_FUNDING_VERIFIED'), ('funding_class', 'ADVERTISER_VERIFIED')), models.Q(('account_type', 'MARKETING_FUNDING_PENDING'), ('funding_class', 'ADVERTISER_UNVERIFIED')), models.Q(('account_type', 'MARKETING_FUNDING_VERIFIED'), ('funding_class', 'ADVERTISER_VERIFIED')), models.Q(('account_type', 'CAMPAIGN_RESTRICTED_FUNDING'), ('funding_class', 'REWARD_RESTRICTED')), models.Q(('account_type', 'REFERRAL_PROGRAMME_FUNDING'), ('funding_class', 'REWARD_RESTRICTED')), models.Q(('account_type', 'REWARD_LIABILITY'), ('funding_class', 'REWARD_RESTRICTED')), models.Q(('account_type', 'MEMBER_REWARD_PENDING'), ('funding_class', 'REWARD_RESTRICTED')), models.Q(('account_type', 'MEMBER_WITHDRAWAL_RESERVED'), ('funding_class', 'REWARD_RESTRICTED')), models.Q(('account_type', 'WITHDRAWAL_CLEARING'), ('funding_class', 'REWARD_RESTRICTED')), models.Q(('account_type', 'REFUNDS_PAYABLE'), ('funding_class', 'OPERATING')), models.Q(('account_type', 'OWNER_EQUITY'), ('funding_class', 'OPERATING')), models.Q(('account_type', 'MEMBERSHIP_REVENUE'), ('funding_class', 'MEMBERSHIP')), models.Q(('account_type', 'ADVERTISING_SERVICE_REVENUE'), ('funding_class', 'OPERATING')), models.Q(('account_type', 'OTHER_OPERATING_REVENUE'), ('funding_class', 'OPERATING')), models.Q(('account_type', 'PAYMENT_PROCESSING_FEES'), ('funding_class', 'OPERATING')), models.Q(('account_type', 'OPERATING_EXPENSES'), ('funding_class', 'OPERATING')), _connector='OR'), name='ledger_account_type_funding_class'),
        ),
        migrations.AddConstraint(
            model_name='ledgeraccount',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('account_type', 'CASH'), ('scope_type', 'PLATFORM')), models.Q(('account_type', 'PAYMENT_CLEARING'), ('scope_type', 'PLATFORM')), models.Q(('account_type', 'ADVERTISER_RECEIVABLE'), ('scope_type', 'ADVERTISER')), models.Q(('account_type', 'MEMBERSHIP_DEFERRED_REVENUE'), ('scope_type', 'PLATFORM')), models.Q(('account_type', 'ADVERTISER_FUNDING_PENDING'), ('scope_type', 'ADVERTISER')), models.Q(('account_type', 'ADVERTISER_FUNDING_VERIFIED'), ('scope_type', 'ADVERTISER')), models.Q(('account_type', 'MARKETING_FUNDING_PENDING'), ('scope_type', 'PLATFORM')), models.Q(('account_type', 'MARKETING_FUNDING_VERIFIED'), ('scope_type', 'PLATFORM')), models.Q(('account_type', 'CAMPAIGN_RESTRICTED_FUNDING'), ('scope_type', 'CAMPAIGN')), models.Q(('account_type', 'REFERRAL_PROGRAMME_FUNDING'), ('scope_type', 'PLATFORM')), models.Q(('account_type', 'REWARD_LIABILITY'), ('scope_type', 'MEMBER')), models.Q(('account_type', 'MEMBER_REWARD_PENDING'), ('scope_type', 'MEMBER')), models.Q(('account_type', 'MEMBER_WITHDRAWAL_RESERVED'), ('scope_type', 'MEMBER')), models.Q(('account_type', 'WITHDRAWAL_CLEARING'), ('scope_type', 'PLATFORM')), models.Q(('account_type', 'REFUNDS_PAYABLE'), ('scope_type', 'PLATFORM')), models.Q(('account_type', 'OWNER_EQUITY'), ('scope_type', 'PLATFORM')), models.Q(('account_type', 'MEMBERSHIP_REVENUE'), ('scope_type', 'PLATFORM')), models.Q(('account_type', 'ADVERTISING_SERVICE_REVENUE'), ('scope_type', 'PLATFORM')), models.Q(('account_type', 'OTHER_OPERATING_REVENUE'), ('scope_type', 'PLATFORM')), models.Q(('account_type', 'PAYMENT_PROCESSING_FEES'), ('scope_type', 'PLATFORM')), models.Q(('account_type', 'OPERATING_EXPENSES'), ('scope_type', 'PLATFORM')), _connector='OR'), name='ledger_account_type_scope'),
        ),
        migrations.AddConstraint(
            model_name='ledgeraccount',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('account_type', 'CASH'), ('overdraft_protected', False)), models.Q(('account_type', 'PAYMENT_CLEARING'), ('overdraft_protected', False)), models.Q(('account_type', 'ADVERTISER_RECEIVABLE'), ('overdraft_protected', False)), models.Q(('account_type', 'MEMBERSHIP_DEFERRED_REVENUE'), ('overdraft_protected', True)), models.Q(('account_type', 'ADVERTISER_FUNDING_PENDING'), ('overdraft_protected', True)), models.Q(('account_type', 'ADVERTISER_FUNDING_VERIFIED'), ('overdraft_protected', True)), models.Q(('account_type', 'MARKETING_FUNDING_PENDING'), ('overdraft_protected', True)), models.Q(('account_type', 'MARKETING_FUNDING_VERIFIED'), ('overdraft_protected', True)), models.Q(('account_type', 'CAMPAIGN_RESTRICTED_FUNDING'), ('overdraft_protected', True)), models.Q(('account_type', 'REFERRAL_PROGRAMME_FUNDING'), ('overdraft_protected', True)), models.Q(('account_type', 'REWARD_LIABILITY'), ('overdraft_protected', True)), models.Q(('account_type', 'MEMBER_REWARD_PENDING'), ('overdraft_protected', True)), models.Q(('account_type', 'MEMBER_WITHDRAWAL_RESERVED'), ('overdraft_protected', True)), models.Q(('account_type', 'WITHDRAWAL_CLEARING'), ('overdraft_protected', True)), models.Q(('account_type', 'REFUNDS_PAYABLE'), ('overdraft_protected', True)), models.Q(('account_type', 'OWNER_EQUITY'), ('overdraft_protected', False)), models.Q(('account_type', 'MEMBERSHIP_REVENUE'), ('overdraft_protected', False)), models.Q(('account_type', 'ADVERTISING_SERVICE_REVENUE'), ('overdraft_protected', False)), models.Q(('account_type', 'OTHER_OPERATING_REVENUE'), ('overdraft_protected', False)), models.Q(('account_type', 'PAYMENT_PROCESSING_FEES'), ('overdraft_protected', False)), models.Q(('account_type', 'OPERATING_EXPENSES'), ('overdraft_protected', False)), _connector='OR'), name='ledger_account_type_protection'),
        ),
        # Accounts only; the reverse is a no-op because ledger accounts are never deleted.
        migrations.RunPython(create_accounts, migrations.RunPython.noop),
        PostgreSQLRunSQL(FORWARD, REVERSE),
        MariaDBRunSQL(MARIADB_FORWARD, MARIADB_REVERSE),
    ]
