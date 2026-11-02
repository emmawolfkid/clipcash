"""
Database-enforced demo reward integrity (PostgreSQL).

* Policies, demo rewards and budget rows are never deleted.
* Policies are created DRAFT; DRAFT -> APPROVED | REJECTED, APPROVED -> RETIRED.
  Campaign, revision, version, amount, currency and author never change, and
  approval details are written once: approved policies are immutable, so past
  demo rewards can never be silently re-priced.
* Demo rewards are created DEMO_QUALIFIED or DEMO_REJECTED; the only change is
  DEMO_QUALIFIED -> DEMO_REVERSED (with reversal details). Session, member,
  campaign, revision, policy, amount and decision never change.
* Simulated consumption never exceeds the campaign's demo budget.

Limitation: the table owner or a superuser can disable these triggers.
"""

from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION rewards_no_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows cannot be deleted', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE OR REPLACE FUNCTION rewards_policy_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'DRAFT' OR NEW.decided_by_id IS NOT NULL OR NEW.effective_from IS NOT NULL THEN
            RAISE EXCEPTION 'reward policies must be created as drafts' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.campaign_id <> OLD.campaign_id OR NEW.campaign_revision <> OLD.campaign_revision
       OR NEW.version <> OLD.version OR NEW.reward_amount <> OLD.reward_amount OR NEW.currency <> OLD.currency
       OR NEW.created_by_id <> OLD.created_by_id OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'reward policy terms cannot change; create a new version'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.decided_by_id IS NOT NULL AND (NEW.decided_by_id IS DISTINCT FROM OLD.decided_by_id
       OR NEW.decided_at IS DISTINCT FROM OLD.decided_at
       OR NEW.effective_from IS DISTINCT FROM OLD.effective_from) THEN
        RAISE EXCEPTION 'reward policy decisions cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.effective_until IS NOT NULL AND NEW.effective_until IS DISTINCT FROM OLD.effective_until THEN
        RAISE EXCEPTION 'reward policy end dates cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status <> OLD.status AND NOT (
        (OLD.status = 'DRAFT' AND NEW.status IN ('APPROVED', 'REJECTED'))
        OR (OLD.status = 'APPROVED' AND NEW.status = 'RETIRED')
    ) THEN
        RAISE EXCEPTION 'invalid reward policy status change % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION rewards_demo_reward_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status NOT IN ('DEMO_QUALIFIED', 'DEMO_REJECTED') OR NEW.reversed_at IS NOT NULL THEN
            RAISE EXCEPTION 'demo rewards are created qualified or rejected' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.session_id <> OLD.session_id OR NEW.member_id <> OLD.member_id OR NEW.campaign_id <> OLD.campaign_id
       OR NEW.campaign_revision <> OLD.campaign_revision OR NEW.policy_id IS DISTINCT FROM OLD.policy_id
       OR NEW.policy_version IS DISTINCT FROM OLD.policy_version OR NEW.amount <> OLD.amount
       OR NEW.currency <> OLD.currency OR NEW.decided_at <> OLD.decided_at
       OR NEW.reasons::text <> OLD.reasons::text THEN
        RAISE EXCEPTION 'demo reward decisions cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status <> OLD.status AND NOT (OLD.status = 'DEMO_QUALIFIED' AND NEW.status = 'DEMO_REVERSED') THEN
        RAISE EXCEPTION 'invalid demo reward status change % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status = 'DEMO_REVERSED' AND (NEW.reversed_at IS DISTINCT FROM OLD.reversed_at
       OR NEW.reversed_by_id IS DISTINCT FROM OLD.reversed_by_id) THEN
        RAISE EXCEPTION 'demo reward reversals cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION rewards_budget_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    allocated bigint;
BEGIN
    IF NEW.demo_consumed > COALESCE(OLD.demo_consumed, 0) OR TG_OP = 'INSERT' THEN
        SELECT demo_budget_amount INTO allocated FROM advertising_campaign WHERE id = NEW.campaign_id;
        IF NEW.demo_consumed > allocated THEN
            RAISE EXCEPTION 'simulated consumption exceeds the campaign demo budget'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER rewards_policy_guard BEFORE INSERT OR UPDATE ON rewards_rewardpolicy
    FOR EACH ROW EXECUTE FUNCTION rewards_policy_guard();
CREATE TRIGGER rewards_demo_reward_guard BEFORE INSERT OR UPDATE ON rewards_demoreward
    FOR EACH ROW EXECUTE FUNCTION rewards_demo_reward_guard();
CREATE TRIGGER rewards_budget_guard BEFORE INSERT OR UPDATE ON rewards_campaignrewardbudget
    FOR EACH ROW EXECUTE FUNCTION rewards_budget_guard();
CREATE TRIGGER rewards_policy_no_delete BEFORE DELETE ON rewards_rewardpolicy
    FOR EACH ROW EXECUTE FUNCTION rewards_no_delete();
CREATE TRIGGER rewards_demo_reward_no_delete BEFORE DELETE ON rewards_demoreward
    FOR EACH ROW EXECUTE FUNCTION rewards_no_delete();
CREATE TRIGGER rewards_budget_no_delete BEFORE DELETE ON rewards_campaignrewardbudget
    FOR EACH ROW EXECUTE FUNCTION rewards_no_delete();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS rewards_budget_no_delete ON rewards_campaignrewardbudget;
DROP TRIGGER IF EXISTS rewards_demo_reward_no_delete ON rewards_demoreward;
DROP TRIGGER IF EXISTS rewards_policy_no_delete ON rewards_rewardpolicy;
DROP TRIGGER IF EXISTS rewards_budget_guard ON rewards_campaignrewardbudget;
DROP TRIGGER IF EXISTS rewards_demo_reward_guard ON rewards_demoreward;
DROP TRIGGER IF EXISTS rewards_policy_guard ON rewards_rewardpolicy;
DROP FUNCTION IF EXISTS rewards_budget_guard();
DROP FUNCTION IF EXISTS rewards_demo_reward_guard();
DROP FUNCTION IF EXISTS rewards_policy_guard();
DROP FUNCTION IF EXISTS rewards_no_delete();
"""


class Migration(migrations.Migration):
    dependencies = [("rewards", "0001_initial"), ("advertising", "0002_integrity")]

    operations = [migrations.RunSQL(FORWARD, REVERSE)]
