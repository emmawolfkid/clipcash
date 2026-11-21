"""
Member wallet integrity and the Phase 09 ZERO-MONEY lock (PostgreSQL).

* No journal line may touch a wallet-linked ledger account. This is the hard
  lock that keeps every member wallet at TZS 0 regardless of feature flags or
  service code. Activating wallet money requires a reviewed migration that
  replaces this trigger; there is no setting that bypasses it.
  (Ledger accounts not linked to a wallet, such as the Phase 02C ledger tests'
  synthetic member accounts, are unaffected, so ledger correctness stays tested.)
* No WalletTransaction row may be written in this phase.
* A wallet links exactly its own member's three accounts of the right types,
  each with zero balance; wallet identity never changes; nothing is deleted.
* Restrictions: kind, reason and author fixed; ACTIVE -> LIFT_REQUESTED ->
  LIFTED (or back to ACTIVE when a lift is declined); LIFTED is final.
* Discrepancies: RESOLVED is final.

Limitation: the table owner or a superuser can disable these triggers.
"""

from django.db import migrations

from apps.core.migration_ops import (
    MariaDBRunSQL,
    PostgreSQLRunSQL,
    changed,
    check_error,
    drop_triggers,
    privilege_error,
    trigger,
)

FORWARD = r"""
CREATE OR REPLACE FUNCTION wallets_block_member_postings() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM wallets_wallet w
               WHERE w.available_account_id = NEW.account_id OR w.pending_account_id = NEW.account_id
                  OR w.reserved_account_id = NEW.account_id) THEN
        RAISE EXCEPTION 'member wallet postings are not permitted: wallets hold no money in this phase'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER a_wallets_block_member_postings BEFORE INSERT ON wallets_journalline
    FOR EACH ROW EXECUTE FUNCTION wallets_block_member_postings();

CREATE OR REPLACE FUNCTION wallets_wallet_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    bad integer;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'wallets cannot be deleted' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF TG_OP = 'INSERT' THEN
        SELECT count(*) INTO bad FROM (
            SELECT NEW.available_account_id AS id, 'REWARD_LIABILITY' AS t
            UNION ALL SELECT NEW.pending_account_id, 'MEMBER_REWARD_PENDING'
            UNION ALL SELECT NEW.reserved_account_id, 'MEMBER_WITHDRAWAL_RESERVED'
        ) wanted
        LEFT JOIN wallets_ledgeraccount a ON a.id = wanted.id
        WHERE a.id IS NULL OR a.account_type <> wanted.t OR a.scope_type <> 'MEMBER'
           OR a.scope_id <> NEW.member_id::text OR a.debit_balance <> 0;
        IF bad > 0 THEN
            RAISE EXCEPTION 'a wallet must link its own member''s empty accounts of the right types'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.number <> OLD.number OR NEW.member_id <> OLD.member_id OR NEW.currency <> OLD.currency
       OR NEW.available_account_id <> OLD.available_account_id
       OR NEW.pending_account_id <> OLD.pending_account_id
       OR NEW.reserved_account_id <> OLD.reserved_account_id OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'wallet identity cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER wallets_wallet_guard BEFORE INSERT OR UPDATE OR DELETE ON wallets_wallet
    FOR EACH ROW EXECUTE FUNCTION wallets_wallet_guard();

CREATE OR REPLACE FUNCTION wallets_transactions_locked() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'wallet transactions are not active in this phase' USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER wallets_transactions_locked BEFORE INSERT OR UPDATE OR DELETE ON wallets_wallettransaction
    FOR EACH ROW EXECUTE FUNCTION wallets_transactions_locked();

CREATE OR REPLACE FUNCTION wallets_restriction_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'wallet restrictions cannot be deleted' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'ACTIVE' THEN
            RAISE EXCEPTION 'restrictions are created active' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.wallet_id <> OLD.wallet_id OR NEW.kind <> OLD.kind OR NEW.reason <> OLD.reason
       OR NEW.placed_by_id <> OLD.placed_by_id OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'restriction details cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status <> OLD.status AND NOT (
        (OLD.status = 'ACTIVE' AND NEW.status = 'LIFT_REQUESTED')
        OR (OLD.status = 'LIFT_REQUESTED' AND NEW.status IN ('LIFTED', 'ACTIVE'))
    ) THEN
        RAISE EXCEPTION 'invalid restriction change % -> %', OLD.status, NEW.status USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER wallets_restriction_guard BEFORE INSERT OR UPDATE OR DELETE ON wallets_walletrestriction
    FOR EACH ROW EXECUTE FUNCTION wallets_restriction_guard();

CREATE OR REPLACE FUNCTION wallets_discrepancy_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'wallet discrepancies cannot be deleted' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.status = 'RESOLVED' AND (NEW.status <> OLD.status OR NEW.resolved_by_id IS DISTINCT FROM OLD.resolved_by_id) THEN
        RAISE EXCEPTION 'resolved discrepancies are final' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER wallets_discrepancy_guard BEFORE UPDATE OR DELETE ON wallets_walletdiscrepancy
    FOR EACH ROW EXECUTE FUNCTION wallets_discrepancy_guard();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS wallets_discrepancy_guard ON wallets_walletdiscrepancy;
DROP TRIGGER IF EXISTS wallets_restriction_guard ON wallets_walletrestriction;
DROP TRIGGER IF EXISTS wallets_transactions_locked ON wallets_wallettransaction;
DROP TRIGGER IF EXISTS wallets_wallet_guard ON wallets_wallet;
DROP TRIGGER IF EXISTS a_wallets_block_member_postings ON wallets_journalline;
DROP FUNCTION IF EXISTS wallets_discrepancy_guard();
DROP FUNCTION IF EXISTS wallets_restriction_guard();
DROP FUNCTION IF EXISTS wallets_transactions_locked();
DROP FUNCTION IF EXISTS wallets_wallet_guard();
DROP FUNCTION IF EXISTS wallets_block_member_postings();
"""


# DB-01: the same rules on MariaDB (one trigger per event). The zero-money lock is a second
# BEFORE INSERT trigger on journal lines, after the ledger's own (0002).
MARIADB_FORWARD = [
    f"""CREATE TRIGGER wallets_block_member_postings BEFORE INSERT ON wallets_journalline FOR EACH ROW
FOLLOWS ledger_line_insert
BEGIN
    IF EXISTS (SELECT 1 FROM wallets_wallet w
               WHERE w.available_account_id = NEW.account_id OR w.pending_account_id = NEW.account_id
                  OR w.reserved_account_id = NEW.account_id) THEN
        {privilege_error("member wallet postings are not permitted: wallets hold no money in this phase")}
    END IF;
END""",
    trigger("wallets_wallet_insert", "BEFORE INSERT", "wallets_wallet", f"""
    IF (SELECT COUNT(*) FROM wallets_ledgeraccount a
         WHERE (a.id = NEW.available_account_id AND BINARY a.account_type = 'REWARD_LIABILITY')
            OR (a.id = NEW.pending_account_id AND BINARY a.account_type = 'MEMBER_REWARD_PENDING')
            OR (a.id = NEW.reserved_account_id AND BINARY a.account_type = 'MEMBER_WITHDRAWAL_RESERVED')) <> 3
       OR EXISTS (SELECT 1 FROM wallets_ledgeraccount a
                   WHERE a.id IN (NEW.available_account_id, NEW.pending_account_id, NEW.reserved_account_id)
                     AND (NOT (BINARY a.scope_type <=> 'MEMBER') OR NOT (BINARY a.scope_id <=> CAST(NEW.member_id AS CHAR))
                          OR a.debit_balance <> 0)) THEN
        {check_error("a wallet must link its own member's empty accounts of the right types")}
    END IF;"""),
    trigger("wallets_wallet_update", "BEFORE UPDATE", "wallets_wallet", f"""
    IF {changed("number", "member_id", "currency", "available_account_id", "pending_account_id",
                "reserved_account_id", "created_at")} THEN
        {privilege_error("wallet identity cannot change")}
    END IF;"""),
    trigger("wallets_wallet_delete", "BEFORE DELETE", "wallets_wallet", privilege_error("wallets cannot be deleted")),
    *[trigger(f"wallets_transactions_locked_{event.lower()}", f"BEFORE {event}", "wallets_wallettransaction",
              privilege_error("wallet transactions are not active in this phase"))
      for event in ("INSERT", "UPDATE", "DELETE")],
    trigger("wallets_restriction_insert", "BEFORE INSERT", "wallets_walletrestriction", f"""
    IF NOT (BINARY NEW.status <=> 'ACTIVE') THEN
        {check_error("restrictions are created active")}
    END IF;"""),
    trigger("wallets_restriction_update", "BEFORE UPDATE", "wallets_walletrestriction", f"""
    DECLARE message VARCHAR(200);
    IF {changed("wallet_id", "kind", "reason", "placed_by_id", "created_at")} THEN
        {privilege_error("restriction details cannot change")}
    END IF;
    IF {changed("status")} AND NOT (
        (BINARY OLD.status = 'ACTIVE' AND BINARY NEW.status = 'LIFT_REQUESTED')
        OR (BINARY OLD.status = 'LIFT_REQUESTED' AND BINARY NEW.status IN ('LIFTED', 'ACTIVE'))
    ) THEN
        SET message = CONCAT('invalid restriction change ', OLD.status, ' -> ', NEW.status);
        SIGNAL SQLSTATE '23000' SET MYSQL_ERRNO = 4025, MESSAGE_TEXT = message;
    END IF;"""),
    trigger("wallets_restriction_delete", "BEFORE DELETE", "wallets_walletrestriction",
            privilege_error("wallet restrictions cannot be deleted")),
    trigger("wallets_discrepancy_update", "BEFORE UPDATE", "wallets_walletdiscrepancy", f"""
    IF BINARY OLD.status = 'RESOLVED' AND {changed("status", "resolved_by_id")} THEN
        {check_error("resolved discrepancies are final")}
    END IF;"""),
    trigger("wallets_discrepancy_delete", "BEFORE DELETE", "wallets_walletdiscrepancy",
            privilege_error("wallet discrepancies cannot be deleted")),
]

MARIADB_REVERSE = drop_triggers(
    "wallets_discrepancy_delete", "wallets_discrepancy_update", "wallets_restriction_delete",
    "wallets_restriction_update", "wallets_restriction_insert", "wallets_transactions_locked_delete",
    "wallets_transactions_locked_update", "wallets_transactions_locked_insert", "wallets_wallet_delete",
    "wallets_wallet_update", "wallets_wallet_insert", "wallets_block_member_postings",
)


class Migration(migrations.Migration):
    dependencies = [("wallets", "0004_member_wallets")]

    operations = [
        PostgreSQLRunSQL(FORWARD, REVERSE),
        MariaDBRunSQL(MARIADB_FORWARD, MARIADB_REVERSE),
    ]
