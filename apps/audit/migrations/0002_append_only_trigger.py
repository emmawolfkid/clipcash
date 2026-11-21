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

from apps.core.migration_ops import MariaDBRunSQL, PostgreSQLRunSQL, drop_triggers, privilege_error, trigger

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


# DB-01: MariaDB. UPDATE is always refused. DELETE is the authorized purge path only, which
# is stricter than on PostgreSQL (where any session may set the flag): all three must hold:
# * the session flag @clipcash_audit_purge = 'on' (set only by the audit_purge command);
# * the client login is the schema owner, the trigger's DEFINER (USER() is the client,
#   CURRENT_USER() the definer inside a trigger), so the runtime login can never purge;
# * the event is older than the minimum retention, a frozen copy of AUDIT_MIN_RETENTION_DAYS
#   (a test checks they agree).
# TRUNCATE needs DROP, which the runtime login does not hold (apps.core.mariadb_logins).
MIN_RETENTION_DAYS = 365

MARIADB_FORWARD = [
    trigger("audit_auditevent_update", "BEFORE UPDATE", "audit_auditevent",
            privilege_error("audit events are append-only (UPDATE blocked)")),
    trigger("audit_auditevent_delete", "BEFORE DELETE", "audit_auditevent", f"""
    IF NOT (@clipcash_audit_purge <=> 'on'
            AND SUBSTRING_INDEX(USER(), '@', 1) = SUBSTRING_INDEX(CURRENT_USER(), '@', 1)
            AND OLD.occurred_at < UTC_TIMESTAMP(6) - INTERVAL {MIN_RETENTION_DAYS} DAY) THEN
        {privilege_error("audit events are append-only (DELETE blocked)")}
    END IF;"""),
]

MARIADB_REVERSE = drop_triggers("audit_auditevent_delete", "audit_auditevent_update")


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
    ]

    operations = [
        PostgreSQLRunSQL(FORWARD, REVERSE),
        MariaDBRunSQL(MARIADB_FORWARD, MARIADB_REVERSE),
    ]
