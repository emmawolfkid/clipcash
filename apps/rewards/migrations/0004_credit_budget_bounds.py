"""
CR-01: awarded credits <= staff credit budget <= advertiser-approved campaign budget.

Two database guards back the service checks (apps.rewards.services.update_credit_terms,
apps.advertising.services.update_campaign / approve_campaign):

* rewards_credit_terms_guard (BEFORE INSERT/UPDATE on rewards_campaigncreditterms): the
  staff credit budget may not exceed the campaign's demo_budget_amount, nor fall below
  the credits already awarded (rewards_campaignrewardbudget.demo_consumed).
* rewards_campaign_budget_bounds (AFTER UPDATE OF demo_budget_amount, revision,
  approved_revision on advertising_campaign): when a revision is approved
  (approved_revision = revision), its budget may not be below the active credit terms
  or the credits already awarded. Drafts are not checked: a proposed budget only applies
  once approved, and approve_campaign lowers the terms to it first, in the same transaction.

The existing rewards_budget_guard already keeps consumption <= the credit budget.
Every existing row satisfies both rules (0003 seeded the terms equal to the approved budget,
and consumption was capped at that budget). Functions pin their search_path (RG-03).
"""

from django.db import migrations

from apps.core.migration_ops import MariaDBRunSQL, PostgreSQLRunSQL, check_error, drop_triggers, trigger

SEARCH_PATH = "pg_catalog, public, pg_temp"
FUNCTIONS = ("rewards_credit_terms_guard()", "rewards_campaign_budget_bounds()")

FORWARD = f"""
CREATE OR REPLACE FUNCTION rewards_credit_terms_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$
DECLARE
    approved bigint;
    consumed bigint;
BEGIN
    SELECT demo_budget_amount INTO approved FROM advertising_campaign WHERE id = NEW.campaign_id;
    IF NEW.credit_budget > approved THEN
        RAISE EXCEPTION 'credit budget exceeds the advertiser-approved campaign budget'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT demo_consumed INTO consumed FROM rewards_campaignrewardbudget WHERE campaign_id = NEW.campaign_id;
    IF NEW.credit_budget < COALESCE(consumed, 0) THEN
        RAISE EXCEPTION 'credit budget is below the credits already awarded' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION rewards_campaign_budget_bounds() RETURNS trigger
LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$
BEGIN
    IF NEW.approved_revision IS NOT NULL AND NEW.approved_revision = NEW.revision THEN
        IF EXISTS (SELECT 1 FROM rewards_campaigncreditterms
                   WHERE campaign_id = NEW.id AND credit_budget > NEW.demo_budget_amount) THEN
            RAISE EXCEPTION 'approved campaign budget is below its active credit budget'
                USING ERRCODE = 'check_violation';
        END IF;
        IF EXISTS (SELECT 1 FROM rewards_campaignrewardbudget
                   WHERE campaign_id = NEW.id AND demo_consumed > NEW.demo_budget_amount) THEN
            RAISE EXCEPTION 'approved campaign budget is below the credits already awarded'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER rewards_credit_terms_guard BEFORE INSERT OR UPDATE ON rewards_campaigncreditterms
    FOR EACH ROW EXECUTE FUNCTION rewards_credit_terms_guard();
CREATE TRIGGER rewards_campaign_budget_bounds
    AFTER UPDATE OF demo_budget_amount, revision, approved_revision ON advertising_campaign
    FOR EACH ROW EXECUTE FUNCTION rewards_campaign_budget_bounds();
"""

REVERSE = """
DROP TRIGGER IF EXISTS rewards_campaign_budget_bounds ON advertising_campaign;
DROP TRIGGER IF EXISTS rewards_credit_terms_guard ON rewards_campaigncreditterms;
DROP FUNCTION IF EXISTS rewards_campaign_budget_bounds();
DROP FUNCTION IF EXISTS rewards_credit_terms_guard();
"""


# DB-01: the same rules on MariaDB (one trigger per event, same messages). Lookups are scalar
# subqueries, so a missing row is NULL exactly as PostgreSQL's SELECT INTO leaves it.
# MariaDB has no column list on UPDATE triggers: the campaign bound is checked on every update
# of an approved revision. That is never looser than PostgreSQL's UPDATE OF (which Django's
# save() always fires, since it writes every column), and the other guards keep the
# invariant true between such updates, so it refuses nothing PostgreSQL would allow.
CREDIT_TERMS_CHECK = f"""
    DECLARE approved BIGINT;
    DECLARE consumed BIGINT;
    SET approved = (SELECT demo_budget_amount FROM advertising_campaign WHERE id = NEW.campaign_id);
    IF NEW.credit_budget > approved THEN
        {check_error("credit budget exceeds the advertiser-approved campaign budget")}
    END IF;
    SET consumed = (SELECT demo_consumed FROM rewards_campaignrewardbudget WHERE campaign_id = NEW.campaign_id);
    IF NEW.credit_budget < COALESCE(consumed, 0) THEN
        {check_error("credit budget is below the credits already awarded")}
    END IF;"""

MARIADB_FORWARD = [
    trigger("rewards_credit_terms_insert", "BEFORE INSERT", "rewards_campaigncreditterms", CREDIT_TERMS_CHECK),
    trigger("rewards_credit_terms_update", "BEFORE UPDATE", "rewards_campaigncreditterms", CREDIT_TERMS_CHECK),
    trigger("rewards_campaign_budget_bounds", "AFTER UPDATE", "advertising_campaign", f"""
    IF NEW.approved_revision IS NOT NULL AND NEW.approved_revision = NEW.revision THEN
        IF EXISTS (SELECT 1 FROM rewards_campaigncreditterms
                   WHERE campaign_id = NEW.id AND credit_budget > NEW.demo_budget_amount) THEN
            {check_error("approved campaign budget is below its active credit budget")}
        END IF;
        IF EXISTS (SELECT 1 FROM rewards_campaignrewardbudget
                   WHERE campaign_id = NEW.id AND demo_consumed > NEW.demo_budget_amount) THEN
            {check_error("approved campaign budget is below the credits already awarded")}
        END IF;
    END IF;"""),
]

MARIADB_REVERSE = drop_triggers(
    "rewards_campaign_budget_bounds", "rewards_credit_terms_update", "rewards_credit_terms_insert")


class Migration(migrations.Migration):
    dependencies = [("rewards", "0003_campaign_credit_terms"), ("advertising", "0002_integrity")]

    operations = [
        PostgreSQLRunSQL(FORWARD, REVERSE),
        MariaDBRunSQL(MARIADB_FORWARD, MARIADB_REVERSE),
    ]
