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


class Migration(migrations.Migration):
    dependencies = [
        ("wallets", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(FORWARD, REVERSE),
    ]
