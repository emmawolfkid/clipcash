-- ClipCash database role separation, step 4 of 4: verify the runtime role.
--
-- Run CONNECTED AS clipcash_app (the runtime credential) to the application database:
--   psql "<clipcash_app connection>" -f scripts/database/roles/04_verify_runtime_role.sql
--
-- Part A executes owner/superuser-level operations, each in its own subtransaction
-- that is forced to roll back even if the operation succeeds, so the script never
-- changes data, triggers, constraints or ownership. Part B checks data privileges
-- with has_*_privilege (no writes; a trigger refusing a write can never be mistaken
-- for a missing privilege). The script ends with an error if any check failed.

\set ON_ERROR_STOP on

DO $verify$
DECLARE
    failures integer := 0;
    probe record;
BEGIN
    -- Part A: owner/superuser-level operations that must be refused.
    FOR probe IN SELECT * FROM (VALUES
        ('disable ledger triggers',       'ALTER TABLE public.wallets_journalline DISABLE TRIGGER USER'),
        ('drop the wallet posting lock',  'DROP TRIGGER a_wallets_block_member_postings ON public.wallets_journalline'),
        ('drop the real-withdrawal lock', 'DROP TRIGGER withdrawals_request_locked ON public.withdrawals_withdrawalrequest'),
        ('drop the audit append-only rule', 'DROP TRIGGER audit_auditevent_append_only ON public.audit_auditevent'),
        ('drop the funding constraint',   'ALTER TABLE public.advertising_campaign DROP CONSTRAINT adv_campaign_funding_locked'),
        ('bypass triggers (replica role)', 'SET LOCAL session_replication_role = replica'),
        ('truncate audit history',        'TRUNCATE public.audit_auditevent'),
        ('create a table',                'CREATE TABLE public.clipcash_role_probe (id integer)'),
        ('replace a trigger function',    'CREATE OR REPLACE FUNCTION public.withdrawals_locked() RETURNS trigger LANGUAGE plpgsql AS ''BEGIN RETURN NEW; END'''),
        -- RG-03: shadowing and escalation.
        ('create a temporary table',      'CREATE TEMPORARY TABLE clipcash_role_probe_tmp (id integer)'),
        ('create a schema',               'CREATE SCHEMA clipcash_role_probe'),
        ('unpin a guard function',        'ALTER FUNCTION public.ledger_verify_entry(uuid) RESET search_path'),
        ('assume the table owner role',   format('SET LOCAL ROLE %I', (SELECT tableowner FROM pg_tables
                                                 WHERE schemaname = 'public' AND tablename = 'wallets_journalline')))
    ) AS p(label, statement) LOOP
        BEGIN
            EXECUTE probe.statement;
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'clipcash_probe_succeeded';
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE NOTICE 'PASS refused: %', probe.label;
            WHEN OTHERS THEN
                failures := failures + 1;
                IF SQLERRM = 'clipcash_probe_succeeded' THEN
                    RAISE WARNING 'FAIL allowed (rolled back): %', probe.label;
                ELSE
                    RAISE WARNING 'FAIL not refused by privileges: % (%: %)', probe.label, SQLSTATE, SQLERRM;
                END IF;
        END;
    END LOOP;

    -- Part B: data privileges (false = must not be held; true = must be held).
    FOR probe IN SELECT * FROM (VALUES
        ('no DELETE on audit history',          NOT has_table_privilege('public.audit_auditevent', 'DELETE')),
        ('no UPDATE on audit history',          NOT has_table_privilege('public.audit_auditevent', 'UPDATE')),
        ('no UPDATE/DELETE on journal entries', NOT has_table_privilege('public.wallets_journalentry', 'UPDATE,DELETE')),
        ('no UPDATE/DELETE on journal lines',   NOT has_table_privilege('public.wallets_journalline', 'UPDATE,DELETE')),
        ('no edit of ledger account identity',  NOT has_column_privilege('public.wallets_ledgeraccount', 'code', 'UPDATE')),
        ('no DELETE on ledger accounts',        NOT has_table_privilege('public.wallets_ledgeraccount', 'DELETE')),
        ('no INSERT of real withdrawals',       NOT has_table_privilege('public.withdrawals_withdrawalrequest', 'INSERT,UPDATE,DELETE')),
        ('no INSERT of wallet transactions',    NOT has_table_privilege('public.wallets_wallettransaction', 'INSERT,UPDATE,DELETE')),
        ('no DELETE on subscriptions',          NOT has_table_privilege('public.memberships_subscription', 'DELETE')),
        ('no write to migration history',       NOT has_table_privilege('public.django_migrations', 'INSERT,UPDATE,DELETE')),
        ('no TRUNCATE anywhere',                NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                                                           WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
                                                           AND has_table_privilege(c.oid, 'TRUNCATE'))),
        ('read accounts',                       has_table_privilege('public.accounts_user', 'SELECT')),
        ('write accounts',                      has_table_privilege('public.accounts_user', 'INSERT,UPDATE')),
        ('append audit events',                 has_table_privilege('public.audit_auditevent', 'INSERT')),
        ('append ledger entries',               has_table_privilege('public.wallets_journalline', 'INSERT')),
        ('maintain ledger balances (trigger)',  has_column_privilege('public.wallets_ledgeraccount', 'debit_balance', 'UPDATE')),
        ('run background jobs',                 has_table_privilege('public.core_job', 'SELECT,INSERT,UPDATE,DELETE')),
        ('owns no table',                       NOT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tableowner = current_user)),
        ('is not a superuser',                  NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)),
        -- RG-03.
        ('not a member of any table owner',     NOT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public'
                                                           AND pg_has_role(current_user, tableowner, 'MEMBER'))),
        ('no CREATE or TEMPORARY on the database', NOT has_database_privilege(current_database(), 'CREATE')
                                                AND NOT has_database_privilege(current_database(), 'TEMPORARY')),
        ('no CREATE in schema public',          NOT has_schema_privilege('public', 'CREATE')),
        ('guard functions pin search_path (migration core.0004)',
                                                EXISTS (SELECT 1 FROM pg_proc WHERE oid = 'public.ledger_verify_entry(uuid)'::regprocedure)
                                                AND NOT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                                                    WHERE n.nspname = 'public'
                                                    AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
                                                    AND NOT coalesce(p.proconfig, '{}') @> ARRAY['search_path=pg_catalog, public, pg_temp'])),
        ('no SECURITY DEFINER functions',       NOT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                                                           WHERE n.nspname = 'public' AND p.prosecdef))
    ) AS p(label, ok) LOOP
        IF probe.ok THEN
            RAISE NOTICE 'PASS %', probe.label;
        ELSE
            failures := failures + 1;
            RAISE WARNING 'FAIL %', probe.label;
        END IF;
    END LOOP;

    IF failures > 0 THEN
        RAISE EXCEPTION 'Runtime role verification failed: % check(s). Do not use this credential for the application.', failures;
    END IF;
    RAISE NOTICE 'Runtime role verification passed.';
END;
$verify$;
