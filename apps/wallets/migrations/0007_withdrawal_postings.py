"""
FIN-02: narrowly extend the wallet lock for manual withdrawals.

* AVAILABLE and PENDING accounts: the four FIN-01 reward postings, plus withdrawal.reserved and
  withdrawal.released on the AVAILABLE account only (PENDING money can never be withdrawn).
* RESERVED (withdrawal) account: only withdrawal.reserved, withdrawal.released and
  withdrawal.paid_manual. Every other posting on any wallet account is still refused.
* WalletTransaction: still insert-only, and only as the exact mirror of an allowed posting line on
  the wallet's own AVAILABLE, PENDING or RESERVED account.
* Ledger balance, overdraft (no negative balances), funding-separation and immutability rules are
  unchanged.

Rollback restores wallets 0006 exactly (withdrawal postings refused again). Posted withdrawal entries
are immutable financial records and remain after a rollback.
"""

from importlib import import_module

from django.db import migrations

from apps.core.migration_ops import MariaDBRunSQL, PostgreSQLRunSQL, check_error, privilege_error, trigger

PREVIOUS = import_module("apps.wallets.migrations.0006_reward_postings")
SEARCH_PATH = "pg_catalog, public, pg_temp"
REWARDS = "'reward.pending', 'reward.cleared', 'reward.pending_reversed', 'reward.cleared_reversed'"
AVAILABLE_WITHDRAWALS = "'withdrawal.reserved', 'withdrawal.released'"
RESERVED_TYPES = "'withdrawal.reserved', 'withdrawal.released', 'withdrawal.paid_manual'"

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
       AND kind NOT IN ({REWARDS}) THEN
        RAISE EXCEPTION 'member wallet postings are limited to reward postings'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF EXISTS (SELECT 1 FROM wallets_wallet w WHERE w.available_account_id = NEW.account_id)
       AND kind NOT IN ({REWARDS}, {AVAILABLE_WITHDRAWALS}) THEN
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
          AND e.posting_type IN ({REWARDS}, {RESERVED_TYPES})
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
       AND NOT (BINARY kind IN ({REWARDS})) THEN
        {privilege_error("member wallet postings are limited to reward postings")}
    END IF;
    IF EXISTS (SELECT 1 FROM wallets_wallet w WHERE w.available_account_id = NEW.account_id)
       AND NOT (BINARY kind IN ({REWARDS}, {AVAILABLE_WITHDRAWALS})) THEN
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
          AND BINARY e.posting_type IN ({REWARDS}, {RESERVED_TYPES})
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

    dependencies = [("wallets", "0006_reward_postings")]

    operations = [
        PostgreSQLRunSQL(FORWARD, REVERSE),
        MariaDBRunSQL(MARIADB_FORWARD, MARIADB_REVERSE),
    ]
