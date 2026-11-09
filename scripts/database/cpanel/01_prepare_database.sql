-- ClipCash on cPanel PostgreSQL, step 1 of 3: close database-level privileges.
--
-- REVIEW BEFORE USE. cPanel cannot create PostgreSQL roles for you, so this
-- variant uses two ordinary cPanel database users instead of the three roles in
-- scripts/database/roles:
--   * the OWNER login: must be (or act as) the owner of the database. It runs
--     migrations, so it owns every table, trigger and trigger function.
--   * the RUNTIME login: the web application and cron. A different cPanel user,
--     never a member of the owner, with data privileges only (step 2).
--
-- Run CONNECTED AS THE OWNER LOGIN to the (empty or existing) ClipCash database,
-- BEFORE the first migration and again after any change in cPanel's
-- "PostgreSQL Databases" page (cPanel grants ALL PRIVILEGES on the database to
-- every user it adds, which this script takes back from the runtime login):
--   psql "<owner connection>" -v runtime_role=<runtime login name> \
--        -f scripts/database/cpanel/01_prepare_database.sql
--
-- Idempotent. It stops without changing anything if the logins cannot give a
-- genuine separation; do NOT continue with a single all-powerful login instead.
--
-- Why CREATE and TEMPORARY are taken away: the trigger functions that protect
-- the ledger and audit history name their tables without a schema, so a login
-- that can create a schema or a temporary table could make a function read a
-- look-alike table. The runtime login gets neither privilege.

\set ON_ERROR_STOP on
\if :{?runtime_role}
\else
  \echo 'Pass -v runtime_role=<runtime login name>'
  \quit 1
\endif

-- Preconditions. Each one stops the script before any change.
SELECT current_setting('server_version_num')::int >= 150000 AS version_ok \gset
\if :version_ok
\else
  \echo 'STOP: PostgreSQL 15 or later is required (unique constraints with NULLS NOT DISTINCT).'
  \quit 1
\endif

SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'runtime_role') AS runtime_exists \gset
\if :runtime_exists
\else
  \echo 'STOP: the runtime login does not exist. Create it in cPanel > PostgreSQL Databases first.'
  \quit 1
\endif

SELECT :'runtime_role' = current_user AS same_login \gset
\if :same_login
  \echo 'STOP: the runtime login must be different from the owner login.'
  \quit 1
\endif

SELECT pg_has_role(current_user, d.datdba, 'MEMBER') AS is_owner
FROM pg_database d WHERE d.datname = current_database() \gset
\if :is_owner
\else
  \echo 'STOP: this login is not the database owner. Connect as the owner login (see netpoa_compatibility.md).'
  \quit 1
\endif

SELECT pg_has_role(:'runtime_role', current_user, 'MEMBER')
    OR pg_has_role(:'runtime_role', d.datdba, 'MEMBER') AS runtime_is_owner
FROM pg_database d WHERE d.datname = current_database() \gset
\if :runtime_is_owner
  \echo 'STOP: the runtime login is (or is a member of) the owner. No separation is possible with it.'
  \quit 1
\endif

SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls AS runtime_powerful
FROM pg_roles WHERE rolname = :'runtime_role' \gset
\if :runtime_powerful
  \echo 'STOP: the runtime login has SUPERUSER, CREATEROLE, CREATEDB, REPLICATION or BYPASSRLS.'
  \quit 1
\endif

BEGIN;

-- Database level: nobody but the owner may create schemas or temporary tables.
SELECT format('REVOKE CREATE, TEMPORARY ON DATABASE %I FROM PUBLIC', current_database()) \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM %I', current_database(), :'runtime_role') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'runtime_role') \gexec

-- Schema public: only the owner creates objects (already the default on PostgreSQL 15+).
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SELECT format('REVOKE CREATE ON SCHEMA public FROM %I', :'runtime_role') \gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'runtime_role') \gexec

COMMIT;

\echo 'Database privileges prepared. Next: migrations as the owner login, then 02_grant_runtime.sql.'
