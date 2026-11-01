"""
Database-level append-only protection for audit events (PostgreSQL).

Blocks every UPDATE, and every DELETE unless the transaction has explicitly set
``clipcash.audit_purge = 'on'`` (only the audit_purge management command does
this). This stops ordinary application code, ORM mistakes and raw SQL issued by
the application from altering history.

Limitation: the table owner (and any superuser) can drop or disable this
trigger, set the purge flag or TRUNCATE the table. Stronger guarantees need
separate database roles in production; see docs/architecture/audit.md.
TRUNCATE is not blocked because Django's test runner flushes tables with it.
"""

from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION audit_auditevent_protect() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('clipcash.audit_purge', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'audit events are append-only (% blocked)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER audit_auditevent_append_only
    BEFORE UPDATE OR DELETE ON audit_auditevent
    FOR EACH ROW EXECUTE FUNCTION audit_auditevent_protect();
"""

REVERSE = """
DROP TRIGGER IF EXISTS audit_auditevent_append_only ON audit_auditevent;
DROP FUNCTION IF EXISTS audit_auditevent_protect();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(FORWARD, REVERSE),
    ]
