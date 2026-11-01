-- ClipCash: PRODUCTION database role separation (TEMPLATE - REVIEW BEFORE USE).
--
-- Not run anywhere by the project. Adapt names, apply as a PostgreSQL
-- administrator during Phase 16 deployment hardening, and set passwords
-- interactively with \password; never put passwords in this file.
--
--   clipcash_owner : owns the schema, runs migrations and retention purges.
--   clipcash_app   : runtime role used by the web application.
--
-- The application connects as clipcash_app; migrations and audit_purge run as
-- clipcash_owner. clipcash_app cannot alter or erase audit history.

\set ON_ERROR_STOP on

CREATE ROLE clipcash_owner LOGIN;
CREATE ROLE clipcash_app LOGIN;

-- CREATE DATABASE clipcash_prod OWNER clipcash_owner ENCODING 'UTF8';
-- \connect clipcash_prod

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO clipcash_app;

-- Run migrations as clipcash_owner, then grant runtime access:
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO clipcash_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO clipcash_app;

-- Audit history: append and read only.
REVOKE UPDATE, DELETE, TRUNCATE ON audit_auditevent FROM clipcash_app;
GRANT SELECT, INSERT ON audit_auditevent TO clipcash_app;

-- Tables created by future migrations (run as clipcash_owner).
ALTER DEFAULT PRIVILEGES FOR ROLE clipcash_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO clipcash_app;
ALTER DEFAULT PRIVILEGES FOR ROLE clipcash_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO clipcash_app;

-- Re-run the audit REVOKE after any migration that recreates audit_auditevent.
-- Future append-only tables (e.g. ledger journal entries) get the same treatment.
