-- ClipCash: provision the disposable PostgreSQL TEST database.
--
-- Run as a PostgreSQL administrator with psql (the password is prompted):
--   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h 127.0.0.1 -d postgres -f scripts\database\create_test_database.sql
--
-- Effects (idempotent; safe to re-run):
--   * creates test_clipcash_db if it does not exist, owned by clipcash_user,
--     with the same encoding and collation as the development database;
--   * ensures the owner is clipcash_user and that no other role may connect.
-- It does NOT modify clipcash_db, grant CREATEDB/SUPERUSER, create roles, or
-- set passwords. Django's test runner reuses this database with --keepdb.

\set ON_ERROR_STOP on

\if :{?app_db}
\else
  \set app_db clipcash_db
\endif
\if :{?test_db}
\else
  \set test_db test_clipcash_db
\endif
\if :{?owner}
\else
  \set owner clipcash_user
\endif

-- Refuse to continue if the names are unsafe or the owner role is missing.
SELECT (:'test_db' = :'app_db' OR :'test_db' NOT LIKE 'test\_%') AS unsafe_name \gset
\if :unsafe_name
  \echo 'Refusing: the test database name must start with test_ and differ from the application database.'
  \quit 1
\endif

SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'owner') AS owner_missing \gset
\if :owner_missing
  \echo 'Refusing: owner role does not exist.'
  \quit 1
\endif

SELECT NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'app_db') AS app_db_missing \gset
\if :app_db_missing
  \echo 'Refusing: application database not found (needed to copy encoding and collation).'
  \quit 1
\endif

-- Create only when missing, copying locale settings from the application database.
SELECT format(
    'CREATE DATABASE %I OWNER %I ENCODING %L LC_COLLATE %L LC_CTYPE %L TEMPLATE template0',
    :'test_db', :'owner', pg_encoding_to_char(d.encoding), d.datcollate, d.datctype
)
FROM pg_database d
WHERE d.datname = :'app_db'
  AND NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'test_db')
\gexec

SELECT format('ALTER DATABASE %I OWNER TO %I', :'test_db', :'owner') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'test_db') \gexec

-- Verification: expect owner = clipcash_user and rolcreatedb/rolsuper = f.
SELECT datname, pg_get_userbyid(datdba) AS owner, pg_encoding_to_char(encoding) AS encoding, datcollate
FROM pg_database WHERE datname IN (:'app_db', :'test_db') ORDER BY datname;
SELECT rolname, rolcreatedb, rolsuper FROM pg_roles WHERE rolname = :'owner';
