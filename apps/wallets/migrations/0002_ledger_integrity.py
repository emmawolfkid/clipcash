"""
Database-enforced ledger integrity (PostgreSQL).

1. ledger_account_guard    - new accounts start at a zero balance; identity
                             columns are immutable; balances change only via
                             journal-line insertion; accounts cannot be deleted.
2. ledger_line_apply       - each inserted line updates its account balance
                             (overdraft check constraints apply immediately,
                             under the row lock, so funds cannot be spent twice)
                             and must target an ACTIVE account of the entry's
                             currency.
3. ledger_immutable        - posted entries and lines cannot be updated or deleted.
4. ledger_verify_entry     - deferred to commit (constraint triggers): every
                             entry has >= 2 lines, debits = credits = total,
                             funding-separation rules hold, and reversals exactly
                             mirror a non-reversal original.

The rule values below are frozen copies of apps.wallets.chart
(PROTECTED_INFLOWS, SEGREGATED_FROM_MEMBERSHIP); a test checks they agree.

Limitation: the table owner or a superuser can disable these triggers. See
docs/architecture/ledger.md for production role separation.
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
-- 1. Accounts ---------------------------------------------------------------
CREATE OR REPLACE FUNCTION ledger_account_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.debit_balance := 0;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'ledger accounts cannot be deleted' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.id <> OLD.id OR NEW.code <> OLD.code OR NEW.account_type <> OLD.account_type
       OR NEW.classification <> OLD.classification OR NEW.funding_class <> OLD.funding_class
       OR NEW.currency <> OLD.currency OR NEW.scope_type <> OLD.scope_type
       OR NEW.scope_id <> OLD.scope_id OR NEW.overdraft_protected <> OLD.overdraft_protected
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'ledger account % identity is immutable', OLD.code
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.debit_balance <> OLD.debit_balance AND pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION 'ledger balances change only by posting journal lines'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER ledger_account_guard
    BEFORE INSERT OR UPDATE OR DELETE ON wallets_ledgeraccount
    FOR EACH ROW EXECUTE FUNCTION ledger_account_guard();

-- 2. Balance maintenance -----------------------------------------------------
CREATE OR REPLACE FUNCTION ledger_line_apply() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    acct_status text;
    acct_currency text;
    entry_currency text;
BEGIN
    SELECT status, currency INTO acct_status, acct_currency
        FROM wallets_ledgeraccount WHERE id = NEW.account_id;
    SELECT currency INTO entry_currency FROM wallets_journalentry WHERE id = NEW.entry_id;
    IF entry_currency IS NULL THEN
        RAISE EXCEPTION 'journal line references a missing entry' USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF acct_status IS DISTINCT FROM 'ACTIVE' THEN
        RAISE EXCEPTION 'cannot post to a % ledger account', coalesce(acct_status, 'missing')
            USING ERRCODE = 'check_violation';
    END IF;
    IF acct_currency <> entry_currency THEN
        RAISE EXCEPTION 'journal line currency does not match its account' USING ERRCODE = 'check_violation';
    END IF;
    UPDATE wallets_ledgeraccount
        SET debit_balance = debit_balance + CASE WHEN NEW.side = 'DEBIT' THEN NEW.amount ELSE -NEW.amount END
        WHERE id = NEW.account_id;
    RETURN NULL;
END;
$$;

CREATE TRIGGER ledger_line_apply
    AFTER INSERT ON wallets_journalline
    FOR EACH ROW EXECUTE FUNCTION ledger_line_apply();

-- 3. Immutability ------------------------------------------------------------
CREATE OR REPLACE FUNCTION ledger_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'posted ledger records are immutable (% on %)', TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER ledger_entry_immutable
    BEFORE UPDATE OR DELETE ON wallets_journalentry
    FOR EACH ROW EXECUTE FUNCTION ledger_immutable();
CREATE TRIGGER ledger_line_immutable
    BEFORE UPDATE OR DELETE ON wallets_journalline
    FOR EACH ROW EXECUTE FUNCTION ledger_immutable();

-- 4. Entry verification at commit ---------------------------------------------
CREATE OR REPLACE FUNCTION ledger_verify_entry(p_entry uuid) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    e record;
    line_count integer;
    debits bigint;
    credits bigint;
    classes text[];
BEGIN
    SELECT * INTO e FROM wallets_journalentry WHERE id = p_entry;
    IF NOT FOUND THEN
        RETURN;  -- Lines without an entry are rejected by the foreign key.
    END IF;

    SELECT count(*),
           coalesce(sum(amount) FILTER (WHERE side = 'DEBIT'), 0),
           coalesce(sum(amount) FILTER (WHERE side = 'CREDIT'), 0)
      INTO line_count, debits, credits
      FROM wallets_journalline WHERE entry_id = p_entry;

    IF line_count < 2 THEN
        RAISE EXCEPTION 'journal entry % must have at least two lines', p_entry USING ERRCODE = 'check_violation';
    END IF;
    IF debits <> credits THEN
        RAISE EXCEPTION 'journal entry % is unbalanced (debits %, credits %)', p_entry, debits, credits
            USING ERRCODE = 'check_violation';
    END IF;
    IF debits <> e.total_amount THEN
        RAISE EXCEPTION 'journal entry % total does not match its lines', p_entry USING ERRCODE = 'check_violation';
    END IF;

    -- Funding separation: membership money never shares an entry with
    -- advertiser or reward money.
    SELECT array_agg(DISTINCT a.funding_class) INTO classes
      FROM wallets_journalline l JOIN wallets_ledgeraccount a ON a.id = l.account_id
     WHERE l.entry_id = p_entry;
    IF 'MEMBERSHIP' = ANY(classes)
       AND classes && ARRAY['ADVERTISER_UNVERIFIED', 'ADVERTISER_VERIFIED', 'REWARD_RESTRICTED'] THEN
        RAISE EXCEPTION 'journal entry % mixes membership and advertiser/reward funds', p_entry
            USING ERRCODE = 'check_violation';
    END IF;

    -- Reward-restricted funds may only be credited from verified advertiser
    -- funds or other reward-restricted accounts.
    IF EXISTS (SELECT 1 FROM wallets_journalline l JOIN wallets_ledgeraccount a ON a.id = l.account_id
                WHERE l.entry_id = p_entry AND l.side = 'CREDIT' AND a.funding_class = 'REWARD_RESTRICTED')
       AND EXISTS (SELECT 1 FROM wallets_journalline l JOIN wallets_ledgeraccount a ON a.id = l.account_id
                    WHERE l.entry_id = p_entry AND l.side = 'DEBIT'
                      AND a.funding_class NOT IN ('ADVERTISER_VERIFIED', 'REWARD_RESTRICTED')) THEN
        RAISE EXCEPTION 'journal entry % funds rewards from a non-verified source', p_entry
            USING ERRCODE = 'check_violation';
    END IF;

    -- Verified advertiser funds may only come from pending (unverified)
    -- advertiser funds or be released back from reward-restricted funds.
    IF EXISTS (SELECT 1 FROM wallets_journalline l JOIN wallets_ledgeraccount a ON a.id = l.account_id
                WHERE l.entry_id = p_entry AND l.side = 'CREDIT' AND a.funding_class = 'ADVERTISER_VERIFIED')
       AND EXISTS (SELECT 1 FROM wallets_journalline l JOIN wallets_ledgeraccount a ON a.id = l.account_id
                    WHERE l.entry_id = p_entry AND l.side = 'DEBIT'
                      AND a.funding_class NOT IN ('ADVERTISER_UNVERIFIED', 'REWARD_RESTRICTED')) THEN
        RAISE EXCEPTION 'journal entry % verifies advertiser funds from an invalid source', p_entry
            USING ERRCODE = 'check_violation';
    END IF;

    -- Reversals: one per original (unique index), never of a reversal, and an
    -- exact mirror of the original's lines.
    IF e.reverses_id IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM wallets_journalentry WHERE id = e.reverses_id AND reverses_id IS NOT NULL) THEN
            RAISE EXCEPTION 'journal entry % reverses a reversal', p_entry USING ERRCODE = 'check_violation';
        END IF;
        IF EXISTS (
            (SELECT account_id, CASE side WHEN 'DEBIT' THEN 'CREDIT' ELSE 'DEBIT' END, amount
               FROM wallets_journalline WHERE entry_id = e.reverses_id
             EXCEPT
             SELECT account_id, side, amount FROM wallets_journalline WHERE entry_id = p_entry)
            UNION ALL
            (SELECT account_id, side, amount FROM wallets_journalline WHERE entry_id = p_entry
             EXCEPT
             SELECT account_id, CASE side WHEN 'DEBIT' THEN 'CREDIT' ELSE 'DEBIT' END, amount
               FROM wallets_journalline WHERE entry_id = e.reverses_id)
        ) THEN
            RAISE EXCEPTION 'journal entry % does not mirror the entry it reverses', p_entry
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ledger_line_deferred_check() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM ledger_verify_entry(NEW.entry_id);
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ledger_entry_deferred_check() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM ledger_verify_entry(NEW.id);
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER ledger_line_balanced
    AFTER INSERT ON wallets_journalline
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger_line_deferred_check();

CREATE CONSTRAINT TRIGGER ledger_entry_complete
    AFTER INSERT ON wallets_journalentry
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger_entry_deferred_check();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS ledger_entry_complete ON wallets_journalentry;
DROP TRIGGER IF EXISTS ledger_line_balanced ON wallets_journalline;
DROP FUNCTION IF EXISTS ledger_entry_deferred_check();
DROP FUNCTION IF EXISTS ledger_line_deferred_check();
DROP FUNCTION IF EXISTS ledger_verify_entry(uuid);
DROP TRIGGER IF EXISTS ledger_line_immutable ON wallets_journalline;
DROP TRIGGER IF EXISTS ledger_entry_immutable ON wallets_journalentry;
DROP FUNCTION IF EXISTS ledger_immutable();
DROP TRIGGER IF EXISTS ledger_line_apply ON wallets_journalline;
DROP FUNCTION IF EXISTS ledger_line_apply();
DROP TRIGGER IF EXISTS ledger_account_guard ON wallets_ledgeraccount;
DROP FUNCTION IF EXISTS ledger_account_guard();
"""


# --- DB-01: MariaDB ------------------------------------------------------------------------
# MariaDB has no deferred (commit-time) triggers, so an entry cannot be checked "at commit".
# Instead every entry goes through an explicit FINALIZATION step:
#
# * entries are inserted as DRAFT; lines may be added only to a DRAFT entry; a DRAFT entry
#   and its lines never touch an account balance;
# * finalization is the single change ever allowed to an entry: UPDATE status DRAFT -> POSTED.
#   Its BEFORE trigger runs the checks PostgreSQL defers to commit (>= 2 lines, debits =
#   credits = total, active same-currency accounts, funding separation, exact reversal mirror,
#   referenced entries posted); its AFTER trigger applies the balances, so the accounts'
#   overdraft CHECK constraints refuse an overdrawing finalization as a whole;
# * a POSTED entry and every line are immutable and nothing is deleted;
# * an account balance may change only to the sum of its lines in POSTED entries. This is an
#   invariant, not a flag: no session can declare itself "finalizing", so a direct UPDATE of a
#   balance can only ever write the value the posted lines already give.
#
# The status CHECK (PostgreSQL: POSTED only) is widened to DRAFT/POSTED on MariaDB. Limitation:
# the migration (owner) login can drop these triggers; the runtime login must not hold TRIGGER,
# DROP, ALTER or CREATE TEMPORARY TABLES (apps.core.mariadb_logins).
SIGNED_AMOUNT = "IF(BINARY {alias}side = 'DEBIT', {alias}amount, -{alias}amount)"
ENTRY_COLUMNS = ("id", "posting_type", "description", "currency", "total_amount", "posted_at", "source_type",
                 "source_id", "external_reference", "related_entry_id", "reverses_id", "idempotency_key",
                 "fingerprint", "actor_id", "request_id")
ACCOUNT_IDENTITY = ("id", "code", "account_type", "classification", "funding_class", "currency", "scope_type",
                    "scope_id", "overdraft_protected", "created_at")
LINES_OF_NEW = ("wallets_journalline l JOIN wallets_ledgeraccount a ON a.id = l.account_id "
                "WHERE l.entry_id = NEW.id")
MIRROR_MISSING = """EXISTS (SELECT 1 FROM wallets_journalline x WHERE x.entry_id = {x} AND NOT EXISTS (
        SELECT 1 FROM wallets_journalline y WHERE y.entry_id = {y} AND y.account_id = x.account_id
           AND y.amount = x.amount AND NOT (BINARY y.side <=> BINARY x.side)))"""

MARIADB_FORWARD = [
    """ALTER TABLE wallets_journalentry DROP CONSTRAINT ledger_entry_status,
       ADD CONSTRAINT ledger_entry_status CHECK (BINARY status IN ('DRAFT', 'POSTED'))""",
    # Accounts ------------------------------------------------------------------------------
    trigger("ledger_account_insert", "BEFORE INSERT", "wallets_ledgeraccount",
            "    SET NEW.debit_balance = 0;"),
    trigger("ledger_account_update", "BEFORE UPDATE", "wallets_ledgeraccount", f"""
    IF {changed(*ACCOUNT_IDENTITY)} THEN
        {privilege_error("ledger account identity is immutable")}
    END IF;
    IF {changed("debit_balance")} AND NOT (NEW.debit_balance <=> (
        SELECT COALESCE(SUM({SIGNED_AMOUNT.format(alias="l.")}), 0)
          FROM wallets_journalline l JOIN wallets_journalentry e ON e.id = l.entry_id
         WHERE l.account_id = NEW.id AND BINARY e.status = 'POSTED')) THEN
        {privilege_error("ledger balances change only by finalizing journal entries")}
    END IF;"""),
    trigger("ledger_account_delete", "BEFORE DELETE", "wallets_ledgeraccount",
            privilege_error("ledger accounts cannot be deleted")),
    # Entries -------------------------------------------------------------------------------
    trigger("ledger_entry_insert", "BEFORE INSERT", "wallets_journalentry", f"""
    IF NOT (BINARY NEW.status <=> 'DRAFT') THEN
        {check_error("journal entries are created as drafts and finalized")}
    END IF;"""),
    trigger("ledger_entry_finalize_check", "BEFORE UPDATE", "wallets_journalentry", f"""
    DECLARE line_count INT;
    DECLARE debits BIGINT;
    DECLARE credits BIGINT;
    IF NOT (BINARY OLD.status <=> 'DRAFT') THEN
        {privilege_error("posted ledger records are immutable (journal entry)")}
    END IF;
    IF NOT (BINARY NEW.status <=> 'POSTED') OR {changed(*ENTRY_COLUMNS)} THEN
        {privilege_error("a draft journal entry can only be finalized")}
    END IF;
    SELECT COUNT(*), COALESCE(SUM(IF(BINARY side = 'DEBIT', amount, 0)), 0),
           COALESCE(SUM(IF(BINARY side = 'CREDIT', amount, 0)), 0)
      INTO line_count, debits, credits FROM wallets_journalline WHERE entry_id = NEW.id;
    IF line_count < 2 THEN
        {check_error("journal entry must have at least two lines")}
    END IF;
    IF debits <> credits THEN
        {check_error("journal entry is unbalanced")}
    END IF;
    IF debits <> NEW.total_amount THEN
        {check_error("journal entry total does not match its lines")}
    END IF;
    IF EXISTS (SELECT 1 FROM {LINES_OF_NEW}
               AND (NOT (BINARY a.status <=> 'ACTIVE') OR NOT (BINARY a.currency <=> BINARY NEW.currency))) THEN
        {check_error("journal entry posts to an inactive account or in another currency")}
    END IF;
    IF EXISTS (SELECT 1 FROM {LINES_OF_NEW} AND BINARY a.funding_class = 'MEMBERSHIP')
       AND EXISTS (SELECT 1 FROM {LINES_OF_NEW}
                    AND BINARY a.funding_class IN ('ADVERTISER_UNVERIFIED', 'ADVERTISER_VERIFIED', 'REWARD_RESTRICTED')) THEN
        {check_error("journal entry mixes membership and advertiser/reward funds")}
    END IF;
    IF EXISTS (SELECT 1 FROM {LINES_OF_NEW} AND BINARY l.side = 'CREDIT' AND BINARY a.funding_class = 'REWARD_RESTRICTED')
       AND EXISTS (SELECT 1 FROM {LINES_OF_NEW} AND BINARY l.side = 'DEBIT'
                    AND BINARY a.funding_class NOT IN ('ADVERTISER_VERIFIED', 'REWARD_RESTRICTED')) THEN
        {check_error("journal entry funds rewards from a non-verified source")}
    END IF;
    IF EXISTS (SELECT 1 FROM {LINES_OF_NEW} AND BINARY l.side = 'CREDIT' AND BINARY a.funding_class = 'ADVERTISER_VERIFIED')
       AND EXISTS (SELECT 1 FROM {LINES_OF_NEW} AND BINARY l.side = 'DEBIT'
                    AND BINARY a.funding_class NOT IN ('ADVERTISER_UNVERIFIED', 'REWARD_RESTRICTED')) THEN
        {check_error("journal entry verifies advertiser funds from an invalid source")}
    END IF;
    IF NEW.related_entry_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM wallets_journalentry r WHERE r.id = NEW.related_entry_id AND BINARY r.status = 'POSTED') THEN
        {check_error("journal entry refers to an entry that is not posted")}
    END IF;
    IF NEW.reverses_id IS NOT NULL THEN
        IF NOT EXISTS (SELECT 1 FROM wallets_journalentry o
                        WHERE o.id = NEW.reverses_id AND BINARY o.status = 'POSTED' AND o.reverses_id IS NULL) THEN
            {check_error("journal entry reverses a reversal or an entry that is not posted")}
        END IF;
        IF {MIRROR_MISSING.format(x="NEW.reverses_id", y="NEW.id")}
           OR {MIRROR_MISSING.format(x="NEW.id", y="NEW.reverses_id")} THEN
            {check_error("journal entry does not mirror the entry it reverses")}
        END IF;
    END IF;"""),
    trigger("ledger_entry_finalize_apply", "AFTER UPDATE", "wallets_journalentry", f"""
    UPDATE wallets_ledgeraccount a
      JOIN (SELECT account_id, SUM({SIGNED_AMOUNT.format(alias="")}) AS delta
              FROM wallets_journalline WHERE entry_id = NEW.id GROUP BY account_id) d ON d.account_id = a.id
       SET a.debit_balance = a.debit_balance + d.delta;"""),
    trigger("ledger_entry_delete", "BEFORE DELETE", "wallets_journalentry",
            privilege_error("posted ledger records are immutable (journal entry)")),
    # Lines ---------------------------------------------------------------------------------
    trigger("ledger_line_insert", "BEFORE INSERT", "wallets_journalline", f"""
    IF NOT EXISTS (SELECT 1 FROM wallets_journalentry e WHERE e.id = NEW.entry_id AND BINARY e.status = 'DRAFT') THEN
        {privilege_error("journal lines can only be added to a draft entry")}
    END IF;"""),
    trigger("ledger_line_update", "BEFORE UPDATE", "wallets_journalline",
            privilege_error("posted ledger records are immutable (journal line)")),
    trigger("ledger_line_delete", "BEFORE DELETE", "wallets_journalline",
            privilege_error("posted ledger records are immutable (journal line)")),
]

MARIADB_REVERSE = drop_triggers(
    "ledger_line_delete", "ledger_line_update", "ledger_line_insert", "ledger_entry_delete",
    "ledger_entry_finalize_apply", "ledger_entry_finalize_check", "ledger_entry_insert", "ledger_account_delete",
    "ledger_account_update", "ledger_account_insert",
) + ["""ALTER TABLE wallets_journalentry DROP CONSTRAINT ledger_entry_status,
        ADD CONSTRAINT ledger_entry_status CHECK (status = 'POSTED')"""]


class Migration(migrations.Migration):
    dependencies = [
        ("wallets", "0001_initial"),
    ]

    operations = [
        PostgreSQLRunSQL(FORWARD, REVERSE),
        MariaDBRunSQL(MARIADB_FORWARD, MARIADB_REVERSE),
    ]
