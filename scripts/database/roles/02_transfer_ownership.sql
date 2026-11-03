-- ClipCash database role separation, step 2 of 4: move ownership to clipcash_owner.
--
-- REVIEW BEFORE USE. Run as a PostgreSQL administrator (or the current owner, if
-- it may grant ownership to clipcash_owner) while connected to the application
-- database, with the application stopped and a fresh backup taken
-- (scripts/database/backup.sh):
--   psql "<admin connection to the app database>" -f scripts/database/roles/02_transfer_ownership.sql
--
-- Moves every table, sequence, view and function in schema public to
-- clipcash_owner and lets clipcash_owner create objects there. Triggers and
-- constraints are not changed (they belong to their tables).

\set ON_ERROR_STOP on

BEGIN;

GRANT USAGE, CREATE ON SCHEMA public TO clipcash_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

SELECT format('ALTER TABLE public.%I OWNER TO clipcash_owner', tablename)
FROM pg_tables WHERE schemaname = 'public' AND tableowner <> 'clipcash_owner' \gexec

SELECT format('ALTER SEQUENCE public.%I OWNER TO clipcash_owner', c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'S' AND pg_get_userbyid(c.relowner) <> 'clipcash_owner'
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype IN ('a', 'i')) \gexec

SELECT format('ALTER VIEW public.%I OWNER TO clipcash_owner', viewname)
FROM pg_views WHERE schemaname = 'public' AND viewowner <> 'clipcash_owner' \gexec

SELECT format('ALTER FUNCTION %s OWNER TO clipcash_owner', p.oid::regprocedure)
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public' AND pg_get_userbyid(p.proowner) <> 'clipcash_owner'
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e') \gexec

COMMIT;

-- Verification: every count must be 0.
SELECT 'tables not owned by clipcash_owner' AS check, count(*) FROM pg_tables
 WHERE schemaname = 'public' AND tableowner <> 'clipcash_owner'
UNION ALL
SELECT 'functions not owned by clipcash_owner', count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'public' AND pg_get_userbyid(p.proowner) <> 'clipcash_owner'
   AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e');
