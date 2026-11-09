-- ClipCash database role separation, step 1 of 4: create the three identities.
--
-- REVIEW BEFORE USE. Run as a PostgreSQL administrator (a role with CREATEROLE)
-- on an isolated staging database first, never directly on production:
--   psql "<admin connection>" -v app_db=clipcash_staging -f scripts/database/roles/01_create_roles.sql
-- Then set passwords interactively (never in files):
--   \password clipcash_migrator
--   \password clipcash_app
--
-- Identities:
--   clipcash_owner    NOLOGIN. Owns every table, sequence and trigger function.
--                     Only the owner (or a superuser) can ALTER/DROP tables or
--                     disable triggers.
--   clipcash_migrator LOGIN. Member of clipcash_owner and acts as it by default
--                     (SET ROLE), so objects it creates are owned by
--                     clipcash_owner. Used only by migrate, createcachetable,
--                     audit_purge and provision_staff_permissions.
--   clipcash_app      LOGIN. Runtime web and worker role. Not a member of the
--                     owner, owns nothing, cannot create objects, gets data
--                     privileges only (step 3).
-- Idempotent: existing roles are left unchanged; database privileges are reset to
-- CONNECT only each time (re-run it once on databases prepared before RG-03).

\set ON_ERROR_STOP on
\if :{?app_db}
\else
  \echo 'Pass -v app_db=<database name>'
  \quit 1
\endif

SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'clipcash_owner') AS create_owner \gset
\if :create_owner
  CREATE ROLE clipcash_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
\endif

SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'clipcash_migrator') AS create_migrator \gset
\if :create_migrator
  CREATE ROLE clipcash_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
      IN ROLE clipcash_owner;
\endif
-- New objects created by migrations belong to clipcash_owner, not to the login role.
ALTER ROLE clipcash_migrator SET role TO 'clipcash_owner';

SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'clipcash_app') AS create_app \gset
\if :create_app
  CREATE ROLE clipcash_app LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
\endif

-- Database-level access: CONNECT only. Nobody but the database owner may create
-- schemas or temporary tables (RG-03: a temporary table shadowed protected tables
-- inside trigger checks until migration core.0004 pinned their search_path; the
-- application never needs temporary tables). Safe to re-run on a database that
-- was prepared by an earlier version of this script, which granted TEMPORARY.
SELECT format('GRANT CONNECT ON DATABASE %I TO clipcash_app, clipcash_migrator', :'app_db') \gexec
SELECT format('REVOKE CREATE, TEMPORARY ON DATABASE %I FROM PUBLIC', :'app_db') \gexec
SELECT format('REVOKE CREATE, TEMPORARY ON DATABASE %I FROM clipcash_app, clipcash_migrator', :'app_db') \gexec

-- Verification.
SELECT rolname, rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolinherit, rolbypassrls
FROM pg_roles WHERE rolname LIKE 'clipcash\_%' ORDER BY rolname;
SELECT r.rolname AS role, m.rolname AS member_of
FROM pg_auth_members am JOIN pg_roles r ON r.oid = am.member JOIN pg_roles m ON m.oid = am.roleid
WHERE r.rolname LIKE 'clipcash\_%';
