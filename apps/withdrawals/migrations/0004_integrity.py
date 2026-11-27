"""
FIN-02 database guards for real withdrawals (PostgreSQL and MariaDB).

WithdrawalRequest (replaces the Phase 10 "refuse every write" lock)
* created REQUESTED, with no decisions or ledger links, for the member's own wallet and destination;
* terms (amounts, fee, settings version, destination snapshot, idempotency key) never change;
* decisions, the external reference and ledger links are written once;
* status: REQUESTED -> UNDER_REVIEW | APPROVED | REJECTED | CANCELLED;
  UNDER_REVIEW -> APPROVED | REJECTED | CANCELLED; APPROVED -> PAYOUT_PENDING | REJECTED | CANCELLED;
  PAYOUT_PENDING -> VERIFYING | RECONCILIATION | FAILED; VERIFYING -> PAID | RECONCILIATION | FAILED;
  RECONCILIATION -> PAID | FAILED. PAID, REJECTED, CANCELLED and FAILED are final;
* ledger links must be the matching withdrawal postings for exactly the wallet deduction;
* never deleted.
Beneficiary bank name and encrypted number are immutable like the other destination details;
settings versions and withdrawal events are append-only; a verified identity check is final.

Rollback drops these guards and restores the Phase 10 lock; it fails while real withdrawal rows
exist (they are financial records and must not be dropped).
"""

from importlib import import_module

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

PHASE10 = import_module("apps.withdrawals.migrations.0002_integrity")
SEARCH_PATH = "pg_catalog, public, pg_temp"
TERMS = ("reference", "member_id", "wallet_id", "beneficiary_id", "settings_version_id", "method", "destination_kind",
         "destination_provider", "destination_holder", "destination_last4", "destination_fingerprint",
         "destination_encrypted", "amount", "fee", "fee_bearer", "wallet_deduction", "net_payout", "currency",
         "idempotency_key", "created_at")
WRITE_ONCE = ("reserve_entry_id", "release_entry_id", "payout_entry_id", "approved_by_id", "approved_at",
              "claimed_by_id", "claimed_at", "payout_reference_lock", "recorded_at", "verified_by_id", "verified_at")
TRANSITIONS = (("REQUESTED", ("UNDER_REVIEW", "APPROVED", "REJECTED", "CANCELLED")),
               ("UNDER_REVIEW", ("APPROVED", "REJECTED", "CANCELLED")),
               ("APPROVED", ("PAYOUT_PENDING", "REJECTED", "CANCELLED")),
               ("PAYOUT_PENDING", ("VERIFYING", "RECONCILIATION", "FAILED")),
               ("VERIFYING", ("PAID", "RECONCILIATION", "FAILED")),
               ("RECONCILIATION", ("PAID", "FAILED")))
ENTRY_KINDS = (("reserve_entry_id", "withdrawal.reserved"), ("release_entry_id", "withdrawal.released"),
               ("payout_entry_id", "withdrawal.paid_manual"))


def _pg_transitions():
    return " OR ".join(
        f"(OLD.status = '{old}' AND NEW.status IN ({', '.join(repr(n) for n in new)}))" for old, new in TRANSITIONS)


def _maria_transitions():
    return " OR ".join(f"(BINARY OLD.status = '{old}' AND BINARY NEW.status IN ({', '.join(repr(n) for n in new)}))"
                       for old, new in TRANSITIONS)


PG_WRITE_ONCE = " OR ".join(f"(OLD.{c} IS NOT NULL AND NEW.{c} IS DISTINCT FROM OLD.{c})" for c in WRITE_ONCE)
PG_ENTRIES = " OR ".join(
    f"(NEW.{c} IS NOT NULL AND OLD.{c} IS NULL AND NOT EXISTS (SELECT 1 FROM wallets_journalentry e "
    f"WHERE e.id = NEW.{c} AND e.posting_type = '{kind}' AND e.total_amount = NEW.wallet_deduction))"
    for c, kind in ENTRY_KINDS)
MARIA_WRITE_ONCE = " OR ".join(f"(OLD.{c} IS NOT NULL AND {changed(c)})" for c in WRITE_ONCE)
MARIA_ENTRIES = " OR ".join(
    f"(NEW.{c} IS NOT NULL AND OLD.{c} IS NULL AND NOT EXISTS (SELECT 1 FROM wallets_journalentry e "
    f"WHERE e.id = NEW.{c} AND BINARY e.posting_type = '{kind}' AND e.total_amount = NEW.wallet_deduction))"
    for c, kind in ENTRY_KINDS)
INSERT_OWNERSHIP = ("NOT EXISTS (SELECT 1 FROM wallets_wallet w WHERE w.id = NEW.wallet_id AND w.member_id = NEW.member_id)"
                    " OR NOT EXISTS (SELECT 1 FROM withdrawals_payoutbeneficiary b WHERE b.id = NEW.beneficiary_id"
                    " AND b.member_id = NEW.member_id)")

FORWARD = rf"""
DROP TRIGGER IF EXISTS withdrawals_request_locked ON withdrawals_withdrawalrequest;

CREATE OR REPLACE FUNCTION withdrawals_request_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'withdrawal requests cannot be deleted' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'REQUESTED' OR NEW.reserve_entry_id IS NOT NULL OR NEW.release_entry_id IS NOT NULL
           OR NEW.payout_entry_id IS NOT NULL OR NEW.approved_by_id IS NOT NULL OR NEW.claimed_by_id IS NOT NULL
           OR NEW.verified_by_id IS NOT NULL OR NEW.payout_reference_lock IS NOT NULL THEN
            RAISE EXCEPTION 'withdrawals must be created as new requests' USING ERRCODE = 'check_violation';
        END IF;
        IF {INSERT_OWNERSHIP} THEN
            RAISE EXCEPTION 'a withdrawal uses the member''s own wallet and destination' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF {" OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in TERMS)} THEN
        RAISE EXCEPTION 'withdrawal terms cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF {PG_WRITE_ONCE} OR (OLD.payout_reference <> '' AND NEW.payout_reference <> OLD.payout_reference) THEN
        RAISE EXCEPTION 'withdrawal decisions and references are written once' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status <> OLD.status AND NOT ({_pg_transitions()}) THEN
        RAISE EXCEPTION 'invalid withdrawal status change % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    IF {PG_ENTRIES} THEN
        RAISE EXCEPTION 'a withdrawal ledger link must be the matching posting' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER withdrawals_request_guard BEFORE INSERT OR UPDATE OR DELETE ON withdrawals_withdrawalrequest
    FOR EACH ROW EXECUTE FUNCTION withdrawals_request_guard();

CREATE OR REPLACE FUNCTION withdrawals_beneficiary_secret_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$
BEGIN
    IF NEW.bank_name <> OLD.bank_name OR NEW.identifier_encrypted <> OLD.identifier_encrypted THEN
        RAISE EXCEPTION 'payout destination details cannot change; add a new destination'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER withdrawals_beneficiary_secret_guard BEFORE UPDATE ON withdrawals_payoutbeneficiary
    FOR EACH ROW EXECUTE FUNCTION withdrawals_beneficiary_secret_guard();

CREATE OR REPLACE FUNCTION withdrawals_identity_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'identity checks cannot be deleted' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.member_id <> OLD.member_id OR OLD.status = 'VERIFIED' THEN
        RAISE EXCEPTION 'a verified identity check is final' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER withdrawals_identity_guard BEFORE UPDATE OR DELETE ON withdrawals_withdrawalidentity
    FOR EACH ROW EXECUTE FUNCTION withdrawals_identity_guard();

CREATE TRIGGER withdrawals_settings_append_only BEFORE UPDATE OR DELETE ON withdrawals_withdrawalsettings
    FOR EACH ROW EXECUTE FUNCTION withdrawals_append_only();
CREATE TRIGGER withdrawals_event_append_only BEFORE UPDATE OR DELETE ON withdrawals_withdrawalevent
    FOR EACH ROW EXECUTE FUNCTION withdrawals_append_only();
"""

REVERSE = """
DROP TRIGGER IF EXISTS withdrawals_event_append_only ON withdrawals_withdrawalevent;
DROP TRIGGER IF EXISTS withdrawals_settings_append_only ON withdrawals_withdrawalsettings;
DROP TRIGGER IF EXISTS withdrawals_identity_guard ON withdrawals_withdrawalidentity;
DROP FUNCTION IF EXISTS withdrawals_identity_guard();
DROP TRIGGER IF EXISTS withdrawals_beneficiary_secret_guard ON withdrawals_payoutbeneficiary;
DROP FUNCTION IF EXISTS withdrawals_beneficiary_secret_guard();
DROP TRIGGER IF EXISTS withdrawals_request_guard ON withdrawals_withdrawalrequest;
DROP FUNCTION IF EXISTS withdrawals_request_guard();
CREATE TRIGGER withdrawals_request_locked BEFORE INSERT OR UPDATE OR DELETE ON withdrawals_withdrawalrequest
    FOR EACH ROW EXECUTE FUNCTION withdrawals_locked();
"""

MARIADB_FORWARD = [
    *drop_triggers("withdrawals_request_locked_insert", "withdrawals_request_locked_update",
                   "withdrawals_request_locked_delete"),
    trigger("withdrawals_request_insert", "BEFORE INSERT", "withdrawals_withdrawalrequest", f"""
    IF NOT (BINARY NEW.status <=> 'REQUESTED') OR NEW.reserve_entry_id IS NOT NULL OR NEW.release_entry_id IS NOT NULL
       OR NEW.payout_entry_id IS NOT NULL OR NEW.approved_by_id IS NOT NULL OR NEW.claimed_by_id IS NOT NULL
       OR NEW.verified_by_id IS NOT NULL OR NEW.payout_reference_lock IS NOT NULL THEN
        {check_error("withdrawals must be created as new requests")}
    END IF;
    IF {INSERT_OWNERSHIP} THEN
        {check_error("a withdrawal uses the member's own wallet and destination")}
    END IF;"""),
    trigger("withdrawals_request_update", "BEFORE UPDATE", "withdrawals_withdrawalrequest", f"""
    DECLARE message VARCHAR(200);
    IF {changed(*TERMS)} THEN
        {privilege_error("withdrawal terms cannot change")}
    END IF;
    IF {MARIA_WRITE_ONCE} OR (OLD.payout_reference <> '' AND {changed("payout_reference")}) THEN
        {privilege_error("withdrawal decisions and references are written once")}
    END IF;
    IF {changed("status")} AND NOT ({_maria_transitions()}) THEN
        SET message = CONCAT('invalid withdrawal status change ', OLD.status, ' -> ', NEW.status);
        SIGNAL SQLSTATE '23000' SET MYSQL_ERRNO = 4025, MESSAGE_TEXT = message;
    END IF;
    IF {MARIA_ENTRIES} THEN
        {check_error("a withdrawal ledger link must be the matching posting")}
    END IF;"""),
    trigger("withdrawals_request_delete", "BEFORE DELETE", "withdrawals_withdrawalrequest",
            privilege_error("withdrawal requests cannot be deleted")),
    trigger("withdrawals_beneficiary_secret_update", "BEFORE UPDATE", "withdrawals_payoutbeneficiary", f"""
    IF {changed("bank_name", "identifier_encrypted")} THEN
        {privilege_error("payout destination details cannot change; add a new destination")}
    END IF;""").replace("FOR EACH ROW\nBEGIN", "FOR EACH ROW FOLLOWS withdrawals_beneficiary_update\nBEGIN"),
    trigger("withdrawals_identity_update", "BEFORE UPDATE", "withdrawals_withdrawalidentity", f"""
    IF {changed("member_id")} OR BINARY OLD.status = 'VERIFIED' THEN
        {privilege_error("a verified identity check is final")}
    END IF;"""),
    trigger("withdrawals_identity_delete", "BEFORE DELETE", "withdrawals_withdrawalidentity",
            privilege_error("identity checks cannot be deleted")),
    *[trigger(f"withdrawals_{name}_{event.lower()}", f"BEFORE {event}", table,
              privilege_error(f"{table} rows are append-only"))
      for name, table in (("settings", "withdrawals_withdrawalsettings"), ("event", "withdrawals_withdrawalevent"))
      for event in ("UPDATE", "DELETE")],
]

_LOCKED = [s for s in PHASE10.MARIADB_FORWARD if s.startswith("CREATE TRIGGER withdrawals_request_locked_")]
MARIADB_REVERSE = drop_triggers(
    "withdrawals_event_delete", "withdrawals_event_update", "withdrawals_settings_delete", "withdrawals_settings_update",
    "withdrawals_identity_delete", "withdrawals_identity_update", "withdrawals_beneficiary_secret_update",
    "withdrawals_request_delete", "withdrawals_request_update", "withdrawals_request_insert") + _LOCKED


class Migration(migrations.Migration):
    dependencies = [("withdrawals", "0003_real_withdrawals"), ("wallets", "0007_withdrawal_postings")]

    operations = [
        PostgreSQLRunSQL(FORWARD, REVERSE),
        MariaDBRunSQL(MARIADB_FORWARD, MARIADB_REVERSE),
    ]
