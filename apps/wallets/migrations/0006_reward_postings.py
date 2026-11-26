"""
FIN-01: narrowly lift the Phase 09 zero-money lock for real rewards only.

* Journal lines on a member's AVAILABLE or PENDING wallet account are accepted only when their
  entry is one of the four reward posting types (reward.pending, reward.cleared,
  reward.pending_reversed, reward.cleared_reversed). Every other posting type, and every line on
  a RESERVED (withdrawal) account, is still refused: withdrawals stay locked.
* WalletTransaction rows can now be INSERTED, and only as the exact projection of a reward posting
  line on that wallet's own account (same entry, bucket, side and amount). UPDATE and DELETE are
  always refused, so wallet history is immutable.
* Balance, overdraft, funding-separation and immutability rules of the ledger (0002) are unchanged.

Rollback: the reverse restores 0005's triggers exactly. Once real reward postings exist, rolling
back does not remove them (posted entries are immutable); it only blocks new ones again.
"""

from importlib import import_module

from django.db import migrations, models

from apps.core.migration_ops import MariaDBRunSQL, PostgreSQLRunSQL, check_error, privilege_error, trigger

PREVIOUS = import_module("apps.wallets.migrations.0005_wallet_integrity")
SEARCH_PATH = "pg_catalog, public, pg_temp"
REWARD_TYPES = ("reward.pending", "reward.cleared", "reward.pending_reversed", "reward.cleared_reversed")
TYPES_SQL = ", ".join(f"'{code}'" for code in REWARD_TYPES)

FORWARD = rf"""
CREATE OR REPLACE FUNCTION wallets_block_member_postings() RETURNS trigger
LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM wallets_wallet w WHERE w.reserved_account_id = NEW.account_id) THEN
        RAISE EXCEPTION 'withdrawal reservations are not permitted: withdrawals are locked'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF EXISTS (SELECT 1 FROM wallets_wallet w
               WHERE w.available_account_id = NEW.account_id OR w.pending_account_id = NEW.account_id)
       AND NOT EXISTS (SELECT 1 FROM wallets_journalentry e
                       WHERE e.id = NEW.entry_id AND e.posting_type IN ({TYPES_SQL})) THEN
        RAISE EXCEPTION 'member wallet postings are limited to reward postings'
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
        WHERE l.entry_id = NEW.journal_entry_id AND e.posting_type IN ({TYPES_SQL}) AND l.amount = NEW.amount
          AND ((NEW.bucket = 'AVAILABLE' AND l.account_id = w.available_account_id)
               OR (NEW.bucket = 'PENDING' AND l.account_id = w.pending_account_id))
          AND ((NEW.direction = 'IN' AND l.side = 'CREDIT') OR (NEW.direction = 'OUT' AND l.side = 'DEBIT'))
    ) THEN
        RAISE EXCEPTION 'a wallet transaction must mirror a reward posting on its own wallet'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
"""


def _section(sql, start, end):
    text = sql[sql.index(start):]
    return text[:text.index(end)]


PINNED = f"LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$"
REVERSE = (_section(PREVIOUS.FORWARD, "CREATE OR REPLACE FUNCTION wallets_block_member_postings()",
                    "CREATE TRIGGER a_wallets_block_member_postings")
           + _section(PREVIOUS.FORWARD, "CREATE OR REPLACE FUNCTION wallets_transactions_locked()",
                      "CREATE TRIGGER wallets_transactions_locked")).replace("LANGUAGE plpgsql AS $$", PINNED)

MARIADB_FORWARD = [
    "DROP TRIGGER IF EXISTS wallets_block_member_postings",
    f"""CREATE TRIGGER wallets_block_member_postings BEFORE INSERT ON wallets_journalline FOR EACH ROW
FOLLOWS ledger_line_insert
BEGIN
    IF EXISTS (SELECT 1 FROM wallets_wallet w WHERE w.reserved_account_id = NEW.account_id) THEN
        {privilege_error("withdrawal reservations are not permitted: withdrawals are locked")}
    END IF;
    IF EXISTS (SELECT 1 FROM wallets_wallet w
               WHERE w.available_account_id = NEW.account_id OR w.pending_account_id = NEW.account_id)
       AND NOT EXISTS (SELECT 1 FROM wallets_journalentry e
                       WHERE e.id = NEW.entry_id AND BINARY e.posting_type IN ({TYPES_SQL})) THEN
        {privilege_error("member wallet postings are limited to reward postings")}
    END IF;
END""",
    "DROP TRIGGER IF EXISTS wallets_transactions_locked_insert",
    trigger("wallets_transactions_locked_insert", "BEFORE INSERT", "wallets_wallettransaction", f"""
    IF NOT EXISTS (
        SELECT 1 FROM wallets_journalline l
        JOIN wallets_journalentry e ON e.id = l.entry_id
        JOIN wallets_wallet w ON w.id = NEW.wallet_id
        WHERE l.entry_id = NEW.journal_entry_id AND BINARY e.posting_type IN ({TYPES_SQL})
          AND l.amount = NEW.amount
          AND ((BINARY NEW.bucket = 'AVAILABLE' AND l.account_id = w.available_account_id)
               OR (BINARY NEW.bucket = 'PENDING' AND l.account_id = w.pending_account_id))
          AND ((BINARY NEW.direction = 'IN' AND BINARY l.side = 'CREDIT')
               OR (BINARY NEW.direction = 'OUT' AND BINARY l.side = 'DEBIT'))
    ) THEN
        {check_error("a wallet transaction must mirror a reward posting on its own wallet")}
    END IF;"""),
]
(_OLD_MARIADB_BLOCK,) = [s for s in PREVIOUS.MARIADB_FORWARD
                         if s.startswith("CREATE TRIGGER wallets_block_member_postings ")]
(_OLD_MARIADB_TXN,) = [s for s in PREVIOUS.MARIADB_FORWARD
                       if s.startswith("CREATE TRIGGER wallets_transactions_locked_insert ")]
MARIADB_REVERSE = ["DROP TRIGGER IF EXISTS wallets_block_member_postings", _OLD_MARIADB_BLOCK,
                   "DROP TRIGGER IF EXISTS wallets_transactions_locked_insert", _OLD_MARIADB_TXN]


class Migration(migrations.Migration):

    dependencies = [
        ('wallets', '0005_wallet_integrity'),
        ('core', '0004_pin_function_search_path'),
    ]

    operations = [
        migrations.AlterField(
            model_name='wallettransaction',
            name='category',
            field=models.CharField(choices=[('REWARD_CREDIT', 'Reward'), ('REWARD_CLEARANCE', 'Reward cleared'), ('WITHDRAWAL_RESERVATION', 'Withdrawal reserved'), ('WITHDRAWAL_SETTLEMENT', 'Withdrawal paid'), ('WITHDRAWAL_RELEASE', 'Withdrawal released'), ('CORRECTION', 'Authorized correction'), ('REVERSAL', 'Reversal')], max_length=24, verbose_name='category'),
        ),
        PostgreSQLRunSQL(FORWARD, REVERSE),
        MariaDBRunSQL(MARIADB_FORWARD, MARIADB_REVERSE),
    ]
