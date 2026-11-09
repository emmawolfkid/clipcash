-- ClipCash on cPanel PostgreSQL, step 2 of 3: least-privilege runtime grants.
--
-- The cPanel counterpart of scripts/database/roles/03_grant_runtime.sql, with the
-- runtime login passed in instead of the fixed name clipcash_app, and the OWNER
-- LOGIN (current_user) in place of clipcash_owner. The table lists must stay
-- identical to that script (apps/core/tests/test_netpoa_deployment.py checks it).
--
-- REVIEW BEFORE USE. Run CONNECTED AS THE OWNER LOGIN (the login that ran the
-- migrations, so it owns every table), after 01_prepare_database.sql and the
-- migrations, and again after every deploy whose migrations add tables:
--   psql "<owner connection>" -v runtime_role=<runtime login name> \
--        -f scripts/database/cpanel/02_grant_runtime.sql
-- Idempotent.
--
-- The runtime login receives data privileges only: never ownership, TRUNCATE,
-- REFERENCES, TRIGGER or CREATE. Without ownership it cannot disable triggers,
-- drop constraints or alter tables; without superuser it cannot set
-- session_replication_role.

\set ON_ERROR_STOP on
\if :{?runtime_role}
\else
  \echo 'Pass -v runtime_role=<runtime login name>'
  \quit 1
\endif

SELECT :'runtime_role' = current_user AS same_login \gset
\if :same_login
  \echo 'STOP: run this as the owner login, not as the runtime login.'
  \quit 1
\endif

SELECT count(*) AS foreign_tables FROM pg_tables
WHERE schemaname = 'public' AND NOT pg_has_role(current_user, tableowner, 'USAGE') \gset
SELECT :foreign_tables = 0 AS owns_all \gset
\if :owns_all
\else
  \echo 'STOP: some tables in schema public are not owned by this login. Run the migrations as this login only.'
  \quit 1
\endif

BEGIN;

SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'runtime_role') \gexec
SELECT format('REVOKE CREATE ON SCHEMA public FROM %I', :'runtime_role') \gexec

-- 1. Baseline data privileges on every table and sequence.
SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO %I', :'runtime_role') \gexec
SELECT format('REVOKE TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public FROM %I', :'runtime_role') \gexec
SELECT format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO %I', :'runtime_role') \gexec

-- Tables created later by migrations run as this owner login.
SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
              :'runtime_role') \gexec
SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO %I',
              :'runtime_role') \gexec

-- 2. No DELETE where a row-level BEFORE DELETE trigger exists: every such trigger
-- in this code base refuses deletes. The audit purge runs as the owner login.
SELECT DISTINCT format('REVOKE DELETE ON TABLE %s FROM %I', t.tgrelid::regclass, :'runtime_role')
FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND NOT t.tgisinternal
  AND (t.tgtype & 1) = 1   -- FOR EACH ROW
  AND (t.tgtype & 2) = 2   -- BEFORE
  AND (t.tgtype & 8) = 8   -- DELETE
\gexec

-- 3. Append-only tables: no UPDATE.
SELECT format('REVOKE UPDATE ON TABLE %s FROM %I', to_regclass(name), :'runtime_role')
FROM unnest(ARRAY[
    'public.audit_auditevent', 'public.wallets_journalentry', 'public.wallets_journalline',
    'public.wallets_wallettransaction', 'public.withdrawals_withdrawalrequest',
    'public.withdrawals_demowithdrawaldecision', 'public.payments_sandboxposting',
    'public.payments_membershipactivationpreview', 'public.memberships_membershipplanversion',
    'public.django_migrations'
]) AS name WHERE to_regclass(name) IS NOT NULL \gexec

-- 4. Locked tables: no INSERT (real wallet transactions and real withdrawals).
SELECT format('REVOKE INSERT ON TABLE %s FROM %I', to_regclass(name), :'runtime_role')
FROM unnest(ARRAY['public.wallets_wallettransaction', 'public.withdrawals_withdrawalrequest',
                  'public.django_migrations']) AS name
WHERE to_regclass(name) IS NOT NULL \gexec

-- Migration history belongs to the owner login.
SELECT format('REVOKE DELETE ON TABLE public.django_migrations FROM %I', :'runtime_role')
WHERE to_regclass('public.django_migrations') IS NOT NULL \gexec

-- 5. Ledger accounts: only status/name changes by the application, and the balance
-- column that the ledger_line_apply trigger maintains (direct balance edits are
-- still refused by ledger_account_guard).
SELECT format('REVOKE UPDATE ON TABLE public.wallets_ledgeraccount FROM %I', :'runtime_role')
WHERE to_regclass('public.wallets_ledgeraccount') IS NOT NULL \gexec
SELECT format('GRANT UPDATE (status, name, updated_at, debit_balance) ON TABLE public.wallets_ledgeraccount TO %I',
              :'runtime_role')
WHERE to_regclass('public.wallets_ledgeraccount') IS NOT NULL \gexec

COMMIT;

\echo 'Runtime grants applied. Next, as the RUNTIME login: 03_verify_separation.sql, then roles/04_verify_runtime_role.sql.'
