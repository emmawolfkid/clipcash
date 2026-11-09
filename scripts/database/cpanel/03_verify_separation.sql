-- ClipCash on cPanel PostgreSQL, step 3 of 3: verify the runtime login (READ-ONLY).
--
-- Run CONNECTED AS THE RUNTIME LOGIN (the credential the application will use):
--   psql "<runtime connection>" -f scripts/database/cpanel/03_verify_separation.sql
--   psql "<runtime connection>" -f scripts/database/roles/04_verify_runtime_role.sql
--
-- This file checks what cPanel can get wrong (database-level CREATE/TEMPORARY,
-- membership in the owner); 04_verify_runtime_role.sql then proves the runtime
-- login cannot disable triggers, truncate or rewrite protected history. Both must
-- pass. This file only reads the system catalogs (read-only session) and ends
-- with an error if any check fails.

\set ON_ERROR_STOP on
SET default_transaction_read_only = on;

DO $verify$
DECLARE
    failures integer := 0;
    probe record;
BEGIN
    FOR probe IN SELECT * FROM (VALUES
        ('PostgreSQL 15 or later',               current_setting('server_version_num')::int >= 150000),
        ('ClipCash tables are present',          to_regclass('public.audit_auditevent') IS NOT NULL
                                                 AND to_regclass('public.wallets_journalline') IS NOT NULL),
        ('btree_gist is installed',              EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'btree_gist')),
        ('not a superuser',                      NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)),
        ('no CREATEROLE / CREATEDB',             NOT (SELECT rolcreaterole OR rolcreatedb FROM pg_roles WHERE rolname = current_user)),
        ('no REPLICATION / BYPASSRLS',           NOT (SELECT rolreplication OR rolbypassrls FROM pg_roles WHERE rolname = current_user)),
        ('not (a member of) the database owner', NOT (SELECT pg_has_role(current_user, d.datdba, 'MEMBER')
                                                      FROM pg_database d WHERE d.datname = current_database())),
        ('not (a member of) any table owner',    NOT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public'
                                                             AND pg_has_role(current_user, tableowner, 'MEMBER'))),
        ('owns no function in public',           NOT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                                                             WHERE n.nspname = 'public'
                                                             AND pg_has_role(current_user, p.proowner, 'MEMBER'))),
        ('cannot CREATE in the database',        NOT has_database_privilege(current_database(), 'CREATE')),
        ('cannot create TEMPORARY tables',       NOT has_database_privilege(current_database(), 'TEMPORARY')),
        ('cannot CREATE in schema public',       NOT has_schema_privilege('public', 'CREATE')),
        ('can CONNECT and use schema public',    has_database_privilege(current_database(), 'CONNECT')
                                                 AND has_schema_privilege('public', 'USAGE'))
    ) AS p(label, ok) LOOP
        IF probe.ok THEN
            RAISE NOTICE 'PASS %', probe.label;
        ELSE
            failures := failures + 1;
            RAISE WARNING 'FAIL %', probe.label;
        END IF;
    END LOOP;

    IF failures > 0 THEN
        RAISE EXCEPTION 'cPanel runtime separation failed: % check(s). Do not use this credential for the application.', failures;
    END IF;
    RAISE NOTICE 'cPanel runtime separation passed. Now run scripts/database/roles/04_verify_runtime_role.sql as this login.';
END;
$verify$;

RESET default_transaction_read_only;
