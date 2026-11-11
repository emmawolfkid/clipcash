-- ClipCash: verify the database protections after migrations, after any cPanel
-- change and after every restore (NET-02). READ-ONLY: catalogue queries only.
--
--   psql "<connection>" -f scripts/database/verify_guards.sql
--
-- Run it as the runtime login (proves the protections are what the application
-- sees) and on every restore-drill database. Each line prints ok = t/f; the
-- script exits with an error unless every check passes. It complements
-- restore_check.sh (whose fixed list predates PAY-01) and the role scripts
-- (03_verify_separation.sql, 04_verify_runtime_role.sql), which it does not replace.
--
-- Checked: the audit, ledger, wallet, withdrawal, advertising, membership and
-- PAY-01 manual-payment guard triggers by name; that NO application trigger is
-- disabled; the key integrity constraints; btree_gist; pinned function
-- search_path (RG-03); no SECURITY DEFINER functions; the PAY-01 migration.

\set ON_ERROR_STOP on

WITH expected_triggers(name) AS (VALUES
    ('audit_auditevent_append_only'),
    ('ledger_account_guard'), ('ledger_line_apply'), ('ledger_entry_immutable'), ('ledger_line_immutable'),
    ('ledger_line_balanced'), ('ledger_entry_complete'),
    ('a_wallets_block_member_postings'), ('wallets_transactions_locked'),
    ('withdrawals_request_locked'), ('advertising_campaign_guard'),
    ('payments_intent_guard'), ('membership_subscription_guard'),
    ('payments_manual_claim_guard'), ('payments_manual_claim_no_delete'),
    ('payments_manual_instruction_guard'), ('payments_manual_instruction_no_delete')
), expected_constraints(name) AS (VALUES
    ('adv_campaign_funding_locked'), ('sub_activation_source_consistent'), ('sub_no_overlapping_periods'),
    ('sub_payment_reference_unique'), ('ledger_entry_external_reference_unique'),
    ('manual_payment_reference_single_use'), ('manual_payment_approval_recorded'),
    ('manual_payment_not_self_reviewed'), ('manual_payment_one_approved_per_subscription')
), app_triggers AS (
    SELECT t.tgname, t.tgenabled FROM pg_trigger t
    JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE NOT t.tgisinternal AND n.nspname = 'public'
), checks(check_name, ok) AS (
    SELECT 'trigger ' || e.name || ' present and enabled',
           EXISTS (SELECT 1 FROM app_triggers a WHERE a.tgname = e.name AND a.tgenabled IN ('O', 'A'))
    FROM expected_triggers e
    UNION ALL
    SELECT 'constraint ' || e.name || ' present',
           EXISTS (SELECT 1 FROM pg_constraint WHERE conname = e.name)
           OR EXISTS (SELECT 1 FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
                      WHERE ic.relname = e.name AND i.indisunique AND i.indisvalid)  -- partial unique index
    FROM expected_constraints e
    UNION ALL
    SELECT 'no application trigger disabled (' || count(*) || ' triggers checked)',
           bool_and(tgenabled IN ('O', 'A')) FROM app_triggers
    UNION ALL
    SELECT 'btree_gist installed', EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'btree_gist')
    UNION ALL
    SELECT 'every application function pins search_path', NOT EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
          AND NOT coalesce(p.proconfig, '{}') @> ARRAY['search_path=pg_catalog, public, pg_temp'])
    UNION ALL
    SELECT 'no SECURITY DEFINER application function', NOT EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.prosecdef
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e'))
    UNION ALL
    SELECT 'migration payments.0005_manual_payment_integrity applied', EXISTS (
        SELECT 1 FROM django_migrations WHERE app = 'payments' AND name = '0005_manual_payment_integrity')
)
SELECT check_name, ok FROM checks ORDER BY ok, check_name;

DO $$
DECLARE failed integer;
BEGIN
    WITH expected_triggers(name) AS (VALUES
        ('audit_auditevent_append_only'),
        ('ledger_account_guard'), ('ledger_line_apply'), ('ledger_entry_immutable'), ('ledger_line_immutable'),
        ('ledger_line_balanced'), ('ledger_entry_complete'),
        ('a_wallets_block_member_postings'), ('wallets_transactions_locked'),
        ('withdrawals_request_locked'), ('advertising_campaign_guard'),
        ('payments_intent_guard'), ('membership_subscription_guard'),
        ('payments_manual_claim_guard'), ('payments_manual_claim_no_delete'),
        ('payments_manual_instruction_guard'), ('payments_manual_instruction_no_delete')
    ), expected_constraints(name) AS (VALUES
        ('adv_campaign_funding_locked'), ('sub_activation_source_consistent'), ('sub_no_overlapping_periods'),
        ('sub_payment_reference_unique'), ('ledger_entry_external_reference_unique'),
        ('manual_payment_reference_single_use'), ('manual_payment_approval_recorded'),
        ('manual_payment_not_self_reviewed'), ('manual_payment_one_approved_per_subscription')
    ), app_triggers AS (
        SELECT t.tgname, t.tgenabled FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE NOT t.tgisinternal AND n.nspname = 'public'
    ), checks(check_name, ok) AS (
        SELECT 'trigger ' || e.name || ' present and enabled',
               EXISTS (SELECT 1 FROM app_triggers a WHERE a.tgname = e.name AND a.tgenabled IN ('O', 'A'))
        FROM expected_triggers e
        UNION ALL
        SELECT 'constraint ' || e.name || ' present',
           EXISTS (SELECT 1 FROM pg_constraint WHERE conname = e.name)
           OR EXISTS (SELECT 1 FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
                      WHERE ic.relname = e.name AND i.indisunique AND i.indisvalid)  -- partial unique index
        FROM expected_constraints e
        UNION ALL
        SELECT 'no application trigger disabled (' || count(*) || ' triggers checked)',
               bool_and(tgenabled IN ('O', 'A')) FROM app_triggers
        UNION ALL
        SELECT 'btree_gist installed', EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'btree_gist')
        UNION ALL
        SELECT 'every application function pins search_path', NOT EXISTS (
            SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public'
              AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
              AND NOT coalesce(p.proconfig, '{}') @> ARRAY['search_path=pg_catalog, public, pg_temp'])
        UNION ALL
        SELECT 'no SECURITY DEFINER application function', NOT EXISTS (
            SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public' AND p.prosecdef
              AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e'))
        UNION ALL
        SELECT 'migration payments.0005_manual_payment_integrity applied', EXISTS (
            SELECT 1 FROM django_migrations WHERE app = 'payments' AND name = '0005_manual_payment_integrity')
    )
    SELECT count(*) INTO failed FROM checks WHERE NOT ok;
    IF failed > 0 THEN
        RAISE EXCEPTION 'guards verification FAILED: % check(s) not satisfied', failed;
    END IF;
    RAISE NOTICE 'guards verification passed';
END
$$;
