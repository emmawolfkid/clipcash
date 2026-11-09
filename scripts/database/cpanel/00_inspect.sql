-- ClipCash on cPanel PostgreSQL, step 0: READ-ONLY inspection of one login.
--
-- Run once with EACH candidate login (the intended migration/owner login and the
-- intended runtime login), connected to the ClipCash database:
--   psql "<connection for one login>" -f scripts/database/cpanel/00_inspect.sql
--
-- It only reads the system catalogs: the session is set read-only first, so it
-- cannot change anything. It prints no password or connection string.
-- Record every row in the compatibility checklist
-- (docs/deployment/netpoa_compatibility.md, section 3).

\set ON_ERROR_STOP on
SET default_transaction_read_only = on;
\pset footer off

\echo '== Server and login'
SELECT current_setting('server_version') AS server_version,
       current_setting('server_version_num')::int >= 150000 AS "version_ok (15+ required)",
       current_database() AS database,
       current_user AS login,
       pg_get_userbyid(d.datdba) AS database_owner,
       pg_has_role(current_user, d.datdba, 'MEMBER') AS login_is_or_acts_as_database_owner
FROM pg_database d WHERE d.datname = current_database();

\echo '== Login attributes (all must be false for the runtime login)'
SELECT rolsuper AS superuser, rolcreaterole AS createrole, rolcreatedb AS createdb,
       rolreplication AS replication, rolbypassrls AS bypassrls, rolinherit AS inherit
FROM pg_roles WHERE rolname = current_user;

\echo '== Roles this login belongs to (runtime login: must not include the table owner)'
SELECT r.rolname AS member_of, m.admin_option
FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid
WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = current_user)
ORDER BY 1;

\echo '== Database privileges of this login and of PUBLIC (runtime: CREATE and TEMPORARY must be false)'
SELECT has_database_privilege(current_database(), 'CONNECT') AS login_connect,
       has_database_privilege(current_database(), 'CREATE') AS login_create,
       has_database_privilege(current_database(), 'TEMPORARY') AS login_temporary,
       EXISTS (SELECT 1 FROM pg_database d, aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) a
               WHERE d.datname = current_database() AND a.grantee = 0 AND a.privilege_type = 'CREATE')
           AS public_create,
       EXISTS (SELECT 1 FROM pg_database d, aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) a
               WHERE d.datname = current_database() AND a.grantee = 0 AND a.privilege_type = 'TEMPORARY')
           AS public_temporary;

\echo '== Schema public (owner login: create must be true; runtime login: false)'
SELECT pg_get_userbyid(n.nspowner) AS schema_owner,
       has_schema_privilege(current_user, 'public', 'USAGE') AS login_usage,
       has_schema_privilege(current_user, 'public', 'CREATE') AS login_create,
       EXISTS (SELECT 1 FROM aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a
               WHERE a.grantee = 0 AND a.privilege_type = 'CREATE') AS public_create
FROM pg_namespace n WHERE n.nspname = 'public';

\echo '== btree_gist (required; must be available and trusted, or already installed)'
SELECT v.name, v.version, v.installed, v.trusted, v.superuser
FROM pg_available_extension_versions v
WHERE v.name = 'btree_gist'
ORDER BY v.installed DESC, v.version DESC
LIMIT 1;
SELECT count(*) AS btree_gist_rows_found FROM pg_available_extension_versions WHERE name = 'btree_gist';

\echo '== Existing objects in schema public (a new database shows 0)'
SELECT count(*) AS tables,
       count(*) FILTER (WHERE tableowner = current_user) AS owned_by_this_login,
       count(*) FILTER (WHERE pg_has_role(current_user, tableowner, 'USAGE')) AS owned_or_actable_by_this_login
FROM pg_tables WHERE schemaname = 'public';

\echo '== Logins with access to this database (from the database ACL)'
SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END AS grantee,
       string_agg(a.privilege_type, ', ' ORDER BY a.privilege_type) AS privileges
FROM pg_database d, aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) a
WHERE d.datname = current_database()
GROUP BY a.grantee ORDER BY 1;

RESET default_transaction_read_only;
