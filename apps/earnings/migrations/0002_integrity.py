"""
FIN-01 database guards for real rewards and campaign funding (PostgreSQL and MariaDB).

SessionReward
* created RESERVED, without ledger entries, for a TZS policy and exactly that policy's amount;
* identity (session, member, campaign, policy, amount, earning day, reservation time) never changes;
* status: RESERVED -> RELEASED | PENDING; PENDING -> AVAILABLE | REVERSED;
  AVAILABLE -> REVERSED | RECOVERY_REQUIRED; RECOVERY_REQUIRED -> REVERSED. RELEASED and REVERSED
  are final;
* each ledger link is set once, to an entry of the right posting type for exactly the amount.

CampaignDeposit
* created RECORDED; identity (organization, method, amount, reference, recorder) never changes;
* RECORDED -> VERIFIED | REJECTED only, and decisions are final;
* the received and verified entries are the matching manual deposit postings for the amount.

FundingMovement: append-only, its entry matches its kind and amount. Nothing here can be deleted.

Rollback drops these guards only; it never removes financial records.
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

SEARCH_PATH = "pg_catalog, public, pg_temp"
REWARD_IDENTITY = ("session_id", "member_id", "campaign_id", "policy_id", "policy_version", "amount", "earning_day",
                   "reserved_at", "created_at")
DEPOSIT_IDENTITY = ("reference", "organization_id", "method", "amount", "external_reference", "reference_key",
                    "paid_on", "recorded_by_id", "created_at")
NO_DELETE = ("earnings_sessionreward", "earnings_campaigndeposit", "earnings_fundingmovement",
             "earnings_earningsettings", "earnings_planearninglimit")

FORWARD = rf"""
CREATE OR REPLACE FUNCTION earnings_no_delete() RETURNS trigger
LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$
BEGIN
    RAISE EXCEPTION '% rows cannot be deleted', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE OR REPLACE FUNCTION earnings_entry_ok(entry uuid, kinds text[], expected bigint) RETURNS boolean
LANGUAGE sql STABLE SET search_path = {SEARCH_PATH} AS $$
    SELECT EXISTS (SELECT 1 FROM wallets_journalentry e
                   WHERE e.id = entry AND e.posting_type = ANY (kinds) AND e.total_amount = expected);
$$;

CREATE OR REPLACE FUNCTION earnings_reward_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'RESERVED' OR NEW.pending_entry_id IS NOT NULL OR NEW.cleared_entry_id IS NOT NULL
           OR NEW.reversal_entry_id IS NOT NULL THEN
            RAISE EXCEPTION 'real rewards must be created as reservations' USING ERRCODE = 'check_violation';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM rewards_rewardpolicy p WHERE p.id = NEW.policy_id AND p.kind = 'TZS'
                       AND p.reward_amount = NEW.amount AND p.version = NEW.policy_version
                       AND p.campaign_id = NEW.campaign_id) THEN
            RAISE EXCEPTION 'a real reward must match its TZS policy' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.session_id <> OLD.session_id OR NEW.member_id <> OLD.member_id OR NEW.campaign_id <> OLD.campaign_id
       OR NEW.policy_id <> OLD.policy_id OR NEW.policy_version <> OLD.policy_version OR NEW.amount <> OLD.amount
       OR NEW.earning_day <> OLD.earning_day OR NEW.reserved_at <> OLD.reserved_at
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'real reward terms cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF (OLD.pending_entry_id IS NOT NULL AND NEW.pending_entry_id IS DISTINCT FROM OLD.pending_entry_id)
       OR (OLD.cleared_entry_id IS NOT NULL AND NEW.cleared_entry_id IS DISTINCT FROM OLD.cleared_entry_id)
       OR (OLD.reversal_entry_id IS NOT NULL AND NEW.reversal_entry_id IS DISTINCT FROM OLD.reversal_entry_id) THEN
        RAISE EXCEPTION 'real reward ledger links cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status <> OLD.status AND NOT (
        (OLD.status = 'RESERVED' AND NEW.status IN ('RELEASED', 'PENDING'))
        OR (OLD.status = 'PENDING' AND NEW.status IN ('AVAILABLE', 'REVERSED'))
        OR (OLD.status = 'AVAILABLE' AND NEW.status IN ('REVERSED', 'RECOVERY_REQUIRED'))
        OR (OLD.status = 'RECOVERY_REQUIRED' AND NEW.status = 'REVERSED')
    ) THEN
        RAISE EXCEPTION 'invalid real reward status change % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.pending_entry_id IS NOT NULL AND OLD.pending_entry_id IS NULL
        AND NOT earnings_entry_ok(NEW.pending_entry_id, ARRAY['reward.pending'], NEW.amount))
       OR (NEW.cleared_entry_id IS NOT NULL AND OLD.cleared_entry_id IS NULL
        AND NOT earnings_entry_ok(NEW.cleared_entry_id, ARRAY['reward.cleared'], NEW.amount))
       OR (NEW.reversal_entry_id IS NOT NULL AND OLD.reversal_entry_id IS NULL
        AND NOT earnings_entry_ok(NEW.reversal_entry_id,
                                  ARRAY['reward.pending_reversed', 'reward.cleared_reversed'], NEW.amount)) THEN
        RAISE EXCEPTION 'a real reward ledger link must be a matching reward posting'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION earnings_deposit_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'RECORDED' THEN
            RAISE EXCEPTION 'deposits must be recorded before they are decided' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.reference <> OLD.reference OR NEW.organization_id <> OLD.organization_id OR NEW.method <> OLD.method
       OR NEW.amount <> OLD.amount OR NEW.external_reference <> OLD.external_reference
       OR NEW.reference_key <> OLD.reference_key OR NEW.paid_on <> OLD.paid_on
       OR NEW.recorded_by_id <> OLD.recorded_by_id OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'deposit details cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.status <> 'RECORDED' AND (NEW.status <> OLD.status
       OR NEW.decided_by_id IS DISTINCT FROM OLD.decided_by_id OR NEW.decided_at IS DISTINCT FROM OLD.decided_at
       OR NEW.received_entry_id IS DISTINCT FROM OLD.received_entry_id
       OR NEW.verified_entry_id IS DISTINCT FROM OLD.verified_entry_id) THEN
        RAISE EXCEPTION 'deposit decisions are final' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status = 'VERIFIED' AND OLD.status = 'RECORDED' AND NOT (
        earnings_entry_ok(NEW.received_entry_id, ARRAY['advertiser.manual_deposit_received'], NEW.amount)
        AND earnings_entry_ok(NEW.verified_entry_id, ARRAY['advertiser.manual_deposit_verified'], NEW.amount)) THEN
        RAISE EXCEPTION 'a verified deposit needs its matching ledger postings' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION earnings_movement_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'funding movements cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NOT earnings_entry_ok(NEW.entry_id, CASE NEW.kind WHEN 'COMMIT' THEN ARRAY['campaign.funding_committed']
                                                       ELSE ARRAY['campaign.funding_released'] END, NEW.amount) THEN
        RAISE EXCEPTION 'a funding movement needs its matching ledger posting' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER earnings_reward_guard BEFORE INSERT OR UPDATE ON earnings_sessionreward
    FOR EACH ROW EXECUTE FUNCTION earnings_reward_guard();
CREATE TRIGGER earnings_deposit_guard BEFORE INSERT OR UPDATE ON earnings_campaigndeposit
    FOR EACH ROW EXECUTE FUNCTION earnings_deposit_guard();
CREATE TRIGGER earnings_movement_guard BEFORE INSERT OR UPDATE ON earnings_fundingmovement
    FOR EACH ROW EXECUTE FUNCTION earnings_movement_guard();
""" + "".join(f"""
CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION earnings_no_delete();
""" for table in NO_DELETE)

REVERSE = "".join(f"DROP TRIGGER IF EXISTS {table}_no_delete ON {table};\n" for table in NO_DELETE) + """
DROP TRIGGER IF EXISTS earnings_movement_guard ON earnings_fundingmovement;
DROP TRIGGER IF EXISTS earnings_deposit_guard ON earnings_campaigndeposit;
DROP TRIGGER IF EXISTS earnings_reward_guard ON earnings_sessionreward;
DROP FUNCTION IF EXISTS earnings_movement_guard();
DROP FUNCTION IF EXISTS earnings_deposit_guard();
DROP FUNCTION IF EXISTS earnings_reward_guard();
DROP FUNCTION IF EXISTS earnings_entry_ok(uuid, text[], bigint);
DROP FUNCTION IF EXISTS earnings_no_delete();
"""


def entry_ok(column, kinds, amount="NEW.amount"):
    """MariaDB: the entry in ``column`` exists with one of ``kinds`` for exactly the amount."""
    listed = ", ".join(f"'{kind}'" for kind in kinds)
    return (f"EXISTS (SELECT 1 FROM wallets_journalentry e WHERE e.id = NEW.{column} "
            f"AND BINARY e.posting_type IN ({listed}) AND e.total_amount = {amount})")


MARIADB_FORWARD = [
    trigger("earnings_reward_insert", "BEFORE INSERT", "earnings_sessionreward", f"""
    IF NOT (BINARY NEW.status <=> 'RESERVED') OR NEW.pending_entry_id IS NOT NULL
       OR NEW.cleared_entry_id IS NOT NULL OR NEW.reversal_entry_id IS NOT NULL THEN
        {check_error("real rewards must be created as reservations")}
    END IF;
    IF NOT EXISTS (SELECT 1 FROM rewards_rewardpolicy p WHERE p.id = NEW.policy_id AND BINARY p.kind = 'TZS'
                   AND p.reward_amount = NEW.amount AND p.version = NEW.policy_version
                   AND p.campaign_id = NEW.campaign_id) THEN
        {check_error("a real reward must match its TZS policy")}
    END IF;"""),
    trigger("earnings_reward_update", "BEFORE UPDATE", "earnings_sessionreward", f"""
    DECLARE message VARCHAR(200);
    IF {changed(*REWARD_IDENTITY)} THEN
        {privilege_error("real reward terms cannot change")}
    END IF;
    IF (OLD.pending_entry_id IS NOT NULL AND {changed("pending_entry_id")})
       OR (OLD.cleared_entry_id IS NOT NULL AND {changed("cleared_entry_id")})
       OR (OLD.reversal_entry_id IS NOT NULL AND {changed("reversal_entry_id")}) THEN
        {privilege_error("real reward ledger links cannot change")}
    END IF;
    IF {changed("status")} AND NOT (
        (BINARY OLD.status = 'RESERVED' AND BINARY NEW.status IN ('RELEASED', 'PENDING'))
        OR (BINARY OLD.status = 'PENDING' AND BINARY NEW.status IN ('AVAILABLE', 'REVERSED'))
        OR (BINARY OLD.status = 'AVAILABLE' AND BINARY NEW.status IN ('REVERSED', 'RECOVERY_REQUIRED'))
        OR (BINARY OLD.status = 'RECOVERY_REQUIRED' AND BINARY NEW.status = 'REVERSED')
    ) THEN
        SET message = CONCAT('invalid real reward status change ', OLD.status, ' -> ', NEW.status);
        SIGNAL SQLSTATE '23000' SET MYSQL_ERRNO = 4025, MESSAGE_TEXT = message;
    END IF;
    IF (NEW.pending_entry_id IS NOT NULL AND OLD.pending_entry_id IS NULL
        AND NOT {entry_ok("pending_entry_id", ["reward.pending"])})
       OR (NEW.cleared_entry_id IS NOT NULL AND OLD.cleared_entry_id IS NULL
        AND NOT {entry_ok("cleared_entry_id", ["reward.cleared"])})
       OR (NEW.reversal_entry_id IS NOT NULL AND OLD.reversal_entry_id IS NULL
        AND NOT {entry_ok("reversal_entry_id", ["reward.pending_reversed", "reward.cleared_reversed"])}) THEN
        {check_error("a real reward ledger link must be a matching reward posting")}
    END IF;"""),
    trigger("earnings_deposit_insert", "BEFORE INSERT", "earnings_campaigndeposit", f"""
    IF NOT (BINARY NEW.status <=> 'RECORDED') THEN
        {check_error("deposits must be recorded before they are decided")}
    END IF;"""),
    trigger("earnings_deposit_update", "BEFORE UPDATE", "earnings_campaigndeposit", f"""
    IF {changed(*DEPOSIT_IDENTITY)} THEN
        {privilege_error("deposit details cannot change")}
    END IF;
    IF NOT (BINARY OLD.status <=> 'RECORDED')
       AND {changed("status", "decided_by_id", "decided_at", "received_entry_id", "verified_entry_id")} THEN
        {privilege_error("deposit decisions are final")}
    END IF;
    IF BINARY NEW.status = 'VERIFIED' AND BINARY OLD.status = 'RECORDED' AND NOT (
        {entry_ok("received_entry_id", ["advertiser.manual_deposit_received"])}
        AND {entry_ok("verified_entry_id", ["advertiser.manual_deposit_verified"])}) THEN
        {check_error("a verified deposit needs its matching ledger postings")}
    END IF;"""),
    trigger("earnings_movement_insert", "BEFORE INSERT", "earnings_fundingmovement", f"""
    IF NOT ((BINARY NEW.kind = 'COMMIT' AND {entry_ok("entry_id", ["campaign.funding_committed"])})
            OR (BINARY NEW.kind = 'RELEASE' AND {entry_ok("entry_id", ["campaign.funding_released"])})) THEN
        {check_error("a funding movement needs its matching ledger posting")}
    END IF;"""),
    trigger("earnings_movement_update", "BEFORE UPDATE", "earnings_fundingmovement",
            privilege_error("funding movements cannot change")),
] + [trigger(f"{table}_no_delete", "BEFORE DELETE", table, privilege_error(f"{table} rows cannot be deleted"))
     for table in NO_DELETE]

MARIADB_REVERSE = drop_triggers(
    *[f"{table}_no_delete" for table in reversed(NO_DELETE)], "earnings_movement_update", "earnings_movement_insert",
    "earnings_deposit_update", "earnings_deposit_insert", "earnings_reward_update", "earnings_reward_insert",
)


class Migration(migrations.Migration):
    dependencies = [("earnings", "0001_initial")]

    operations = [
        PostgreSQLRunSQL(FORWARD, REVERSE),
        MariaDBRunSQL(MARIADB_FORWARD, MARIADB_REVERSE),
    ]
