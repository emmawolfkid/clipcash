-- ClipCash database role separation, step 3 of 4: least-privilege runtime grants.
--
-- REVIEW BEFORE USE. Run as clipcash_migrator (acting as clipcash_owner) or an
-- administrator, connected to the application database, after step 2 and again
-- after every deploy whose migrations add tables:
--   psql "<migrator connection>" -f scripts/database/roles/03_grant_runtime.sql
-- Idempotent.
--
-- clipcash_app receives data privileges only: never ownership, TRUNCATE,
-- REFERENCES, TRIGGER or CREATE. Without ownership it cannot disable triggers,
-- drop constraints or alter tables; without superuser it cannot set
-- session_replication_role. On top of the database triggers, writes the
-- triggers already forbid are also refused by privileges (same SQLSTATE 42501).

\set ON_ERROR_STOP on

BEGIN;

GRANT USAGE ON SCHEMA public TO clipcash_app;
REVOKE CREATE ON SCHEMA public FROM clipcash_app;

-- 1. Baseline data privileges on every table and sequence.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO clipcash_app;
REVOKE TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public FROM clipcash_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO clipcash_app;

-- Tables created later by migrations (owned by clipcash_owner).
ALTER DEFAULT PRIVILEGES FOR ROLE clipcash_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO clipcash_app;
ALTER DEFAULT PRIVILEGES FOR ROLE clipcash_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO clipcash_app;

-- 2. No DELETE where a row-level BEFORE DELETE trigger exists: every such trigger
-- in this code base refuses deletes (ledger, wallets, withdrawals, payments,
-- rewards, advertising, memberships, audit). The audit purge runs as the owner.
SELECT DISTINCT format('REVOKE DELETE ON TABLE %s FROM clipcash_app', t.tgrelid::regclass)
FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND NOT t.tgisinternal
  AND (t.tgtype & 1) = 1   -- FOR EACH ROW
  AND (t.tgtype & 2) = 2   -- BEFORE
  AND (t.tgtype & 8) = 8   -- DELETE
\gexec

-- 3. Append-only tables: no UPDATE.
SELECT format('REVOKE UPDATE ON TABLE %s FROM clipcash_app', to_regclass(name))
FROM unnest(ARRAY[
    'public.audit_auditevent', 'public.wallets_journalentry', 'public.wallets_journalline',
    'public.wallets_wallettransaction', 'public.withdrawals_withdrawalrequest',
    'public.withdrawals_demowithdrawaldecision', 'public.payments_sandboxposting',
    'public.payments_membershipactivationpreview', 'public.memberships_membershipplanversion',
    'public.django_migrations'
]) AS name WHERE to_regclass(name) IS NOT NULL \gexec

-- 4. Locked tables: no INSERT (real wallet transactions and real withdrawals).
SELECT format('REVOKE INSERT ON TABLE %s FROM clipcash_app', to_regclass(name))
FROM unnest(ARRAY['public.wallets_wallettransaction', 'public.withdrawals_withdrawalrequest',
                  'public.django_migrations']) AS name
WHERE to_regclass(name) IS NOT NULL \gexec

-- 5. Ledger accounts: only status/name changes by the application, and the balance
-- column that the ledger_line_apply trigger maintains (it runs with the inserting
-- role's privileges; direct balance edits are still refused by ledger_account_guard).
DO $$
BEGIN
    IF to_regclass('public.wallets_ledgeraccount') IS NOT NULL THEN
        REVOKE UPDATE ON TABLE public.wallets_ledgeraccount FROM clipcash_app;
        GRANT UPDATE (status, name, updated_at, debit_balance) ON TABLE public.wallets_ledgeraccount TO clipcash_app;
    END IF;
END;
$$;

COMMIT;
