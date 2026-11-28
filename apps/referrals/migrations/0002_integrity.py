"""
REF-01 database guards for the referral programme (PostgreSQL and MariaDB).

ReferralCode: never changes and is never deleted.

ReferralPolicy
* created PROPOSED (undecided); only the built-in version 1, which offers no money, starts ACTIVE;
* a proposal's terms never change; PROPOSED -> ACTIVE | REJECTED once, and decisions are final
  (four-eyes is a CHECK constraint: the decider is never the proposer).

Referral
* recorded only at registration: the referred account must be less than an hour old, the code must be
  the referrer's own code, the policy an approved version, and the referred member must not already
  have referred the referrer (no circular pair). Self-referral is a CHECK constraint;
* identity (reference, referrer, referred, code, source, policy) never changes;
* status: AWAITING -> UNDER_REVIEW | QUALIFIED | REWARD_PENDING | EXPIRED | REJECTED;
  UNDER_REVIEW -> QUALIFIED | REWARD_PENDING | REJECTED; QUALIFIED -> REWARD_PENDING | REJECTED;
  REWARD_PENDING -> REWARD_CLEARED | REVERSED; REWARD_CLEARED -> REVERSED. Others are final.

ReferralReward
* created PENDING for the referral's referrer, with its referral.reward_pending entry for exactly the
  amount, never above the referral's policy amount and with that policy's version;
* terms never change; PENDING -> AVAILABLE | REVERSED; AVAILABLE -> REVERSED | RECOVERY_REQUIRED;
  RECOVERY_REQUIRED -> REVERSED; each ledger link is set once, to a matching referral posting.

MarketingDeposit: as FIN-01 campaign deposits (recorded, then a final decision with its postings).
ReferralFundingMovement: append-only; its entry matches its kind and amount.
Nothing here can be deleted. Rollback drops these guards only; it never removes records.
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
POLICY_TERMS = ("version", "base_version", "tracking_open", "rewards_open", "reward_amount",
                "referrer_requires_membership", "referred_requires_membership", "min_account_age_days",
                "required_verified_views", "required_view_days", "qualification_days", "daily_reward_limit",
                "lifetime_reward_limit", "clearance_hours", "reason", "created_by_id", "created_at")
REFERRAL_IDENTITY = ("reference", "referrer_id", "referred_id", "code", "source", "policy_id", "created_at")
REWARD_IDENTITY = ("referral_id", "member_id", "policy_version", "amount", "reward_day", "clears_at",
                   "pending_entry_id", "created_at")
DEPOSIT_IDENTITY = ("reference", "method", "amount", "external_reference", "reference_key", "paid_on",
                    "recorded_by_id", "created_at")
NO_DELETE = ("referrals_referralcode", "referrals_referralpolicy", "referrals_referral", "referrals_referralreward",
             "referrals_marketingdeposit", "referrals_referralfundingmovement")
TRANSITIONS = (
    ("AWAITING", ("UNDER_REVIEW", "QUALIFIED", "REWARD_PENDING", "EXPIRED", "REJECTED")),
    ("UNDER_REVIEW", ("QUALIFIED", "REWARD_PENDING", "REJECTED")),
    ("QUALIFIED", ("REWARD_PENDING", "REJECTED")),
    ("REWARD_PENDING", ("REWARD_CLEARED", "REVERSED")),
    ("REWARD_CLEARED", ("REVERSED",)),
)
REWARD_TRANSITIONS = (
    ("PENDING", ("AVAILABLE", "REVERSED")),
    ("AVAILABLE", ("REVERSED", "RECOVERY_REQUIRED")),
    ("RECOVERY_REQUIRED", ("REVERSED",)),
)


def _listed(values):
    return ", ".join(f"'{value}'" for value in values)


def _allowed(rules, binary=""):
    return " OR ".join(f"({binary}OLD.status = '{old}' AND {binary}NEW.status IN ({_listed(new)}))"
                       for old, new in rules)


def _pg_changed(*columns):
    return " OR ".join(f"NEW.{column} IS DISTINCT FROM OLD.{column}" for column in columns)


FORWARD = rf"""
CREATE OR REPLACE FUNCTION referrals_no_delete() RETURNS trigger
LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$
BEGIN
    RAISE EXCEPTION '% rows cannot be deleted', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE OR REPLACE FUNCTION referrals_entry_ok(entry uuid, kinds text[], expected bigint) RETURNS boolean
LANGUAGE sql STABLE SET search_path = {SEARCH_PATH} AS $$
    SELECT EXISTS (SELECT 1 FROM wallets_journalentry e
                   WHERE e.id = entry AND e.posting_type = ANY (kinds) AND e.total_amount = expected);
$$;

CREATE OR REPLACE FUNCTION referrals_code_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$
BEGIN
    RAISE EXCEPTION 'referral codes cannot change' USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE OR REPLACE FUNCTION referrals_policy_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NOT (NEW.status = 'PROPOSED' AND NEW.decided_by_id IS NULL AND NEW.decided_at IS NULL)
           AND NOT (NEW.status = 'ACTIVE' AND NEW.version = 1 AND NOT NEW.rewards_open AND NEW.reward_amount = 0
                    AND NEW.decided_by_id IS NULL) THEN
            RAISE EXCEPTION 'referral policy versions must be proposed and approved' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF {_pg_changed(*POLICY_TERMS)} THEN
        RAISE EXCEPTION 'referral policy terms cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.status <> 'PROPOSED' AND ({_pg_changed("status", "decided_by_id", "decided_at", "decision_note")}) THEN
        RAISE EXCEPTION 'referral policy decisions are final' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION referrals_referral_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'AWAITING' OR NEW.qualified_at IS NOT NULL OR NEW.reviewed_by_id IS NOT NULL
           OR NEW.outcome_reason <> '' THEN
            RAISE EXCEPTION 'referrals must start awaiting qualification' USING ERRCODE = 'check_violation';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM accounts_user u WHERE u.id = NEW.referred_id
                       AND u.date_joined >= now() - interval '1 hour') THEN
            RAISE EXCEPTION 'referrals are recorded only at registration' USING ERRCODE = 'check_violation';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM referrals_referralcode c WHERE c.member_id = NEW.referrer_id
                       AND c.code = NEW.code) THEN
            RAISE EXCEPTION 'a referral must use the referrer''s own code' USING ERRCODE = 'check_violation';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM referrals_referralpolicy p WHERE p.id = NEW.policy_id AND p.status = 'ACTIVE') THEN
            RAISE EXCEPTION 'a referral needs an approved policy version' USING ERRCODE = 'check_violation';
        END IF;
        IF EXISTS (SELECT 1 FROM referrals_referral r WHERE r.referrer_id = NEW.referred_id
                   AND r.referred_id = NEW.referrer_id) THEN
            RAISE EXCEPTION 'circular referrals are not allowed' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF {_pg_changed(*REFERRAL_IDENTITY)} THEN
        RAISE EXCEPTION 'referral attribution cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.qualified_at IS NOT NULL AND NEW.qualified_at IS DISTINCT FROM OLD.qualified_at THEN
        RAISE EXCEPTION 'a referral qualification time cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status <> OLD.status AND NOT ({_allowed(TRANSITIONS)}) THEN
        RAISE EXCEPTION 'invalid referral status change % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION referrals_reward_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'PENDING' OR NEW.cleared_entry_id IS NOT NULL OR NEW.reversal_entry_id IS NOT NULL THEN
            RAISE EXCEPTION 'referral rewards must start pending' USING ERRCODE = 'check_violation';
        END IF;
        IF NOT referrals_entry_ok(NEW.pending_entry_id, ARRAY['referral.reward_pending'], NEW.amount) THEN
            RAISE EXCEPTION 'a referral reward needs its matching ledger posting' USING ERRCODE = 'check_violation';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM referrals_referral r JOIN referrals_referralpolicy p ON p.id = r.policy_id
                       WHERE r.id = NEW.referral_id AND r.referrer_id = NEW.member_id
                         AND p.version = NEW.policy_version AND NEW.amount <= p.reward_amount
                         AND r.status IN ('AWAITING', 'UNDER_REVIEW', 'QUALIFIED')) THEN
            RAISE EXCEPTION 'a referral reward must follow its referral and policy' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF {_pg_changed(*REWARD_IDENTITY)} THEN
        RAISE EXCEPTION 'referral reward terms cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF (OLD.cleared_entry_id IS NOT NULL AND NEW.cleared_entry_id IS DISTINCT FROM OLD.cleared_entry_id)
       OR (OLD.reversal_entry_id IS NOT NULL AND NEW.reversal_entry_id IS DISTINCT FROM OLD.reversal_entry_id) THEN
        RAISE EXCEPTION 'referral reward ledger links cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status <> OLD.status AND NOT ({_allowed(REWARD_TRANSITIONS)}) THEN
        RAISE EXCEPTION 'invalid referral reward status change % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.cleared_entry_id IS NOT NULL AND OLD.cleared_entry_id IS NULL
        AND NOT referrals_entry_ok(NEW.cleared_entry_id, ARRAY['referral.reward_cleared'], NEW.amount))
       OR (NEW.reversal_entry_id IS NOT NULL AND OLD.reversal_entry_id IS NULL
        AND NOT referrals_entry_ok(NEW.reversal_entry_id,
                                   ARRAY['referral.pending_reversed', 'referral.cleared_reversed'], NEW.amount)) THEN
        RAISE EXCEPTION 'a referral reward ledger link must be a matching referral posting'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION referrals_deposit_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'RECORDED' THEN
            RAISE EXCEPTION 'deposits must be recorded before they are decided' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF {_pg_changed(*DEPOSIT_IDENTITY)} THEN
        RAISE EXCEPTION 'deposit details cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.status <> 'RECORDED' AND ({_pg_changed("status", "decided_by_id", "decided_at", "received_entry_id",
                                                  "verified_entry_id")}) THEN
        RAISE EXCEPTION 'deposit decisions are final' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status = 'VERIFIED' AND OLD.status = 'RECORDED' AND NOT (
        referrals_entry_ok(NEW.received_entry_id, ARRAY['marketing.deposit_received'], NEW.amount)
        AND referrals_entry_ok(NEW.verified_entry_id, ARRAY['marketing.deposit_verified'], NEW.amount)) THEN
        RAISE EXCEPTION 'a verified deposit needs its matching ledger postings' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION referrals_movement_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'referral budget movements cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NOT referrals_entry_ok(NEW.entry_id, CASE NEW.kind WHEN 'COMMIT' THEN ARRAY['referral.funding_committed']
                                                        ELSE ARRAY['referral.funding_released'] END, NEW.amount) THEN
        RAISE EXCEPTION 'a referral budget movement needs its matching ledger posting'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER referrals_code_guard BEFORE UPDATE ON referrals_referralcode
    FOR EACH ROW EXECUTE FUNCTION referrals_code_guard();
CREATE TRIGGER referrals_policy_guard BEFORE INSERT OR UPDATE ON referrals_referralpolicy
    FOR EACH ROW EXECUTE FUNCTION referrals_policy_guard();
CREATE TRIGGER referrals_referral_guard BEFORE INSERT OR UPDATE ON referrals_referral
    FOR EACH ROW EXECUTE FUNCTION referrals_referral_guard();
CREATE TRIGGER referrals_reward_guard BEFORE INSERT OR UPDATE ON referrals_referralreward
    FOR EACH ROW EXECUTE FUNCTION referrals_reward_guard();
CREATE TRIGGER referrals_deposit_guard BEFORE INSERT OR UPDATE ON referrals_marketingdeposit
    FOR EACH ROW EXECUTE FUNCTION referrals_deposit_guard();
CREATE TRIGGER referrals_movement_guard BEFORE INSERT OR UPDATE ON referrals_referralfundingmovement
    FOR EACH ROW EXECUTE FUNCTION referrals_movement_guard();
""" + "".join(f"""
CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION referrals_no_delete();
""" for table in NO_DELETE)

REVERSE = "".join(f"DROP TRIGGER IF EXISTS {table}_no_delete ON {table};\n" for table in NO_DELETE) + """
DROP TRIGGER IF EXISTS referrals_movement_guard ON referrals_referralfundingmovement;
DROP TRIGGER IF EXISTS referrals_deposit_guard ON referrals_marketingdeposit;
DROP TRIGGER IF EXISTS referrals_reward_guard ON referrals_referralreward;
DROP TRIGGER IF EXISTS referrals_referral_guard ON referrals_referral;
DROP TRIGGER IF EXISTS referrals_policy_guard ON referrals_referralpolicy;
DROP TRIGGER IF EXISTS referrals_code_guard ON referrals_referralcode;
DROP FUNCTION IF EXISTS referrals_movement_guard();
DROP FUNCTION IF EXISTS referrals_deposit_guard();
DROP FUNCTION IF EXISTS referrals_reward_guard();
DROP FUNCTION IF EXISTS referrals_referral_guard();
DROP FUNCTION IF EXISTS referrals_policy_guard();
DROP FUNCTION IF EXISTS referrals_code_guard();
DROP FUNCTION IF EXISTS referrals_entry_ok(uuid, text[], bigint);
DROP FUNCTION IF EXISTS referrals_no_delete();
"""


def entry_ok(column, kinds, amount="NEW.amount"):
    """MariaDB: the entry in ``column`` exists with one of ``kinds`` for exactly the amount."""
    return (f"EXISTS (SELECT 1 FROM wallets_journalentry e WHERE e.id = NEW.{column} "
            f"AND BINARY e.posting_type IN ({_listed(kinds)}) AND e.total_amount = {amount})")


def status_change(rules, label):
    return f"""IF {changed("status")} AND NOT ({_allowed(rules, "BINARY ")}) THEN
        SET message = CONCAT('invalid {label} status change ', OLD.status, ' -> ', NEW.status);
        SIGNAL SQLSTATE '23000' SET MYSQL_ERRNO = 4025, MESSAGE_TEXT = message;
    END IF;"""


MARIADB_FORWARD = [
    trigger("referrals_code_update", "BEFORE UPDATE", "referrals_referralcode",
            privilege_error("referral codes cannot change")),
    trigger("referrals_policy_insert", "BEFORE INSERT", "referrals_referralpolicy", f"""
    IF NOT (BINARY NEW.status <=> 'PROPOSED' AND NEW.decided_by_id IS NULL AND NEW.decided_at IS NULL)
       AND NOT (BINARY NEW.status <=> 'ACTIVE' AND NEW.version = 1 AND NEW.rewards_open = 0
                AND NEW.reward_amount = 0 AND NEW.decided_by_id IS NULL) THEN
        {check_error("referral policy versions must be proposed and approved")}
    END IF;"""),
    trigger("referrals_policy_update", "BEFORE UPDATE", "referrals_referralpolicy", f"""
    IF {changed(*POLICY_TERMS)} THEN
        {privilege_error("referral policy terms cannot change")}
    END IF;
    IF NOT (BINARY OLD.status <=> 'PROPOSED') AND {changed("status", "decided_by_id", "decided_at", "decision_note")} THEN
        {privilege_error("referral policy decisions are final")}
    END IF;"""),
    trigger("referrals_referral_insert", "BEFORE INSERT", "referrals_referral", f"""
    IF NOT (BINARY NEW.status <=> 'AWAITING') OR NEW.qualified_at IS NOT NULL OR NEW.reviewed_by_id IS NOT NULL
       OR NOT (NEW.outcome_reason <=> '') THEN
        {check_error("referrals must start awaiting qualification")}
    END IF;
    IF NOT EXISTS (SELECT 1 FROM accounts_user u WHERE u.id = NEW.referred_id
                   AND u.date_joined >= UTC_TIMESTAMP(6) - INTERVAL 1 HOUR) THEN
        {check_error("referrals are recorded only at registration")}
    END IF;
    IF NOT EXISTS (SELECT 1 FROM referrals_referralcode c WHERE c.member_id = NEW.referrer_id
                   AND BINARY c.code = BINARY NEW.code) THEN
        {check_error("a referral must use the referrer's own code")}
    END IF;
    IF NOT EXISTS (SELECT 1 FROM referrals_referralpolicy p WHERE p.id = NEW.policy_id
                   AND BINARY p.status = 'ACTIVE') THEN
        {check_error("a referral needs an approved policy version")}
    END IF;
    IF EXISTS (SELECT 1 FROM referrals_referral r WHERE r.referrer_id = NEW.referred_id
               AND r.referred_id = NEW.referrer_id) THEN
        {check_error("circular referrals are not allowed")}
    END IF;"""),
    trigger("referrals_referral_update", "BEFORE UPDATE", "referrals_referral", f"""
    DECLARE message VARCHAR(200);
    IF {changed(*REFERRAL_IDENTITY)} THEN
        {privilege_error("referral attribution cannot change")}
    END IF;
    IF OLD.qualified_at IS NOT NULL AND {changed("qualified_at")} THEN
        {privilege_error("a referral qualification time cannot change")}
    END IF;
    {status_change(TRANSITIONS, "referral")}"""),
    trigger("referrals_reward_insert", "BEFORE INSERT", "referrals_referralreward", f"""
    IF NOT (BINARY NEW.status <=> 'PENDING') OR NEW.cleared_entry_id IS NOT NULL
       OR NEW.reversal_entry_id IS NOT NULL THEN
        {check_error("referral rewards must start pending")}
    END IF;
    IF NOT {entry_ok("pending_entry_id", ["referral.reward_pending"])} THEN
        {check_error("a referral reward needs its matching ledger posting")}
    END IF;
    IF NOT EXISTS (SELECT 1 FROM referrals_referral r JOIN referrals_referralpolicy p ON p.id = r.policy_id
                   WHERE r.id = NEW.referral_id AND r.referrer_id = NEW.member_id
                     AND p.version = NEW.policy_version AND NEW.amount <= p.reward_amount
                     AND BINARY r.status IN ('AWAITING', 'UNDER_REVIEW', 'QUALIFIED')) THEN
        {check_error("a referral reward must follow its referral and policy")}
    END IF;"""),
    trigger("referrals_reward_update", "BEFORE UPDATE", "referrals_referralreward", f"""
    DECLARE message VARCHAR(200);
    IF {changed(*REWARD_IDENTITY)} THEN
        {privilege_error("referral reward terms cannot change")}
    END IF;
    IF (OLD.cleared_entry_id IS NOT NULL AND {changed("cleared_entry_id")})
       OR (OLD.reversal_entry_id IS NOT NULL AND {changed("reversal_entry_id")}) THEN
        {privilege_error("referral reward ledger links cannot change")}
    END IF;
    {status_change(REWARD_TRANSITIONS, "referral reward")}
    IF (NEW.cleared_entry_id IS NOT NULL AND OLD.cleared_entry_id IS NULL
        AND NOT {entry_ok("cleared_entry_id", ["referral.reward_cleared"])})
       OR (NEW.reversal_entry_id IS NOT NULL AND OLD.reversal_entry_id IS NULL
        AND NOT {entry_ok("reversal_entry_id", ["referral.pending_reversed", "referral.cleared_reversed"])}) THEN
        {check_error("a referral reward ledger link must be a matching referral posting")}
    END IF;"""),
    trigger("referrals_deposit_insert", "BEFORE INSERT", "referrals_marketingdeposit", f"""
    IF NOT (BINARY NEW.status <=> 'RECORDED') THEN
        {check_error("deposits must be recorded before they are decided")}
    END IF;"""),
    trigger("referrals_deposit_update", "BEFORE UPDATE", "referrals_marketingdeposit", f"""
    IF {changed(*DEPOSIT_IDENTITY)} THEN
        {privilege_error("deposit details cannot change")}
    END IF;
    IF NOT (BINARY OLD.status <=> 'RECORDED')
       AND {changed("status", "decided_by_id", "decided_at", "received_entry_id", "verified_entry_id")} THEN
        {privilege_error("deposit decisions are final")}
    END IF;
    IF BINARY NEW.status = 'VERIFIED' AND BINARY OLD.status = 'RECORDED' AND NOT (
        {entry_ok("received_entry_id", ["marketing.deposit_received"])}
        AND {entry_ok("verified_entry_id", ["marketing.deposit_verified"])}) THEN
        {check_error("a verified deposit needs its matching ledger postings")}
    END IF;"""),
    trigger("referrals_movement_insert", "BEFORE INSERT", "referrals_referralfundingmovement", f"""
    IF NOT ((BINARY NEW.kind = 'COMMIT' AND {entry_ok("entry_id", ["referral.funding_committed"])})
            OR (BINARY NEW.kind = 'RELEASE' AND {entry_ok("entry_id", ["referral.funding_released"])})) THEN
        {check_error("a referral budget movement needs its matching ledger posting")}
    END IF;"""),
    trigger("referrals_movement_update", "BEFORE UPDATE", "referrals_referralfundingmovement",
            privilege_error("referral budget movements cannot change")),
] + [trigger(f"{table}_no_delete", "BEFORE DELETE", table, privilege_error(f"{table} rows cannot be deleted"))
     for table in NO_DELETE]

MARIADB_REVERSE = drop_triggers(
    *[f"{table}_no_delete" for table in reversed(NO_DELETE)], "referrals_movement_update",
    "referrals_movement_insert", "referrals_deposit_update", "referrals_deposit_insert", "referrals_reward_update",
    "referrals_reward_insert", "referrals_referral_update", "referrals_referral_insert", "referrals_policy_update",
    "referrals_policy_insert", "referrals_code_update",
)


class Migration(migrations.Migration):
    dependencies = [("referrals", "0001_initial"), ("wallets", "0008_referral_accounts")]

    operations = [
        PostgreSQLRunSQL(FORWARD, REVERSE),
        MariaDBRunSQL(MARIADB_FORWARD, MARIADB_REVERSE),
    ]
