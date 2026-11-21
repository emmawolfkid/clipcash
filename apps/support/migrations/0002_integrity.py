"""
Database-enforced support ticket integrity (PostgreSQL), Phase 15-B.

* Tickets are created OPEN; reference, requester, organization, submission key and
  creation time never change.
* Status follows the state machine:
      OPEN, IN_PROGRESS, WAITING_FOR_CUSTOMER -> any other open state, RESOLVED or CLOSED
      RESOLVED -> OPEN | IN_PROGRESS (reopened) | CLOSED
      CLOSED   -> final
* Messages and history events are append-only.
* Nothing is deleted.

Role rules (who may make which change) are enforced by apps.support.services.
Limitation: the table owner or a superuser can disable these triggers.
"""

from django.db import migrations

from apps.core.migration_ops import (
    MariaDBRunSQL,
    PostgreSQLRunSQL,
    changed,
    check_error,
    drop_triggers,
    privilege_error,
    trigger,
)

FORWARD = r"""
CREATE OR REPLACE FUNCTION support_ticket_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'support tickets cannot be deleted' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'OPEN' OR NEW.resolved_at IS NOT NULL OR NEW.closed_at IS NOT NULL THEN
            RAISE EXCEPTION 'support tickets are created open' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.reference <> OLD.reference OR NEW.requester_id <> OLD.requester_id
       OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
       OR NEW.submission_key <> OLD.submission_key OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'support ticket identity cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status <> OLD.status AND NOT (
        (OLD.status IN ('OPEN', 'IN_PROGRESS', 'WAITING_FOR_CUSTOMER')
            AND NEW.status IN ('OPEN', 'IN_PROGRESS', 'WAITING_FOR_CUSTOMER', 'RESOLVED', 'CLOSED'))
        OR (OLD.status = 'RESOLVED' AND NEW.status IN ('OPEN', 'IN_PROGRESS', 'CLOSED'))
    ) THEN
        RAISE EXCEPTION 'invalid support ticket status change % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status = 'CLOSED' THEN
        RAISE EXCEPTION 'closed support tickets cannot change' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER support_ticket_guard BEFORE INSERT OR UPDATE OR DELETE ON support_ticket
    FOR EACH ROW EXECUTE FUNCTION support_ticket_guard();

CREATE OR REPLACE FUNCTION support_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER support_message_append_only BEFORE UPDATE OR DELETE ON support_ticketmessage
    FOR EACH ROW EXECUTE FUNCTION support_append_only();
CREATE TRIGGER support_event_append_only BEFORE UPDATE OR DELETE ON support_ticketevent
    FOR EACH ROW EXECUTE FUNCTION support_append_only();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS support_event_append_only ON support_ticketevent;
DROP TRIGGER IF EXISTS support_message_append_only ON support_ticketmessage;
DROP FUNCTION IF EXISTS support_append_only();
DROP TRIGGER IF EXISTS support_ticket_guard ON support_ticket;
DROP FUNCTION IF EXISTS support_ticket_guard();
"""


# DB-01: the same rules on MariaDB (one trigger per event).
MARIADB_FORWARD = [
    trigger("support_ticket_insert", "BEFORE INSERT", "support_ticket", f"""
    IF NOT (BINARY NEW.status <=> 'OPEN') OR NEW.resolved_at IS NOT NULL OR NEW.closed_at IS NOT NULL THEN
        {check_error("support tickets are created open")}
    END IF;"""),
    trigger("support_ticket_update", "BEFORE UPDATE", "support_ticket", f"""
    DECLARE message VARCHAR(200);
    IF {changed("reference", "requester_id", "organization_id", "submission_key", "created_at")} THEN
        {privilege_error("support ticket identity cannot change")}
    END IF;
    IF {changed("status")} AND NOT (
        (BINARY OLD.status IN ('OPEN', 'IN_PROGRESS', 'WAITING_FOR_CUSTOMER')
            AND BINARY NEW.status IN ('OPEN', 'IN_PROGRESS', 'WAITING_FOR_CUSTOMER', 'RESOLVED', 'CLOSED'))
        OR (BINARY OLD.status = 'RESOLVED' AND BINARY NEW.status IN ('OPEN', 'IN_PROGRESS', 'CLOSED'))
    ) THEN
        SET message = CONCAT('invalid support ticket status change ', OLD.status, ' -> ', NEW.status);
        SIGNAL SQLSTATE '23000' SET MYSQL_ERRNO = 4025, MESSAGE_TEXT = message;
    END IF;
    IF BINARY OLD.status = 'CLOSED' THEN
        {check_error("closed support tickets cannot change")}
    END IF;"""),
    trigger("support_ticket_delete", "BEFORE DELETE", "support_ticket",
            privilege_error("support tickets cannot be deleted")),
] + [
    trigger(f"support_{name}_{event.lower()}", f"BEFORE {event}", table, privilege_error(f"{table} rows are append-only"))
    for name, table in (("message", "support_ticketmessage"), ("event", "support_ticketevent"))
    for event in ("UPDATE", "DELETE")
]

MARIADB_REVERSE = drop_triggers(
    "support_event_delete", "support_event_update", "support_message_delete", "support_message_update",
    "support_ticket_delete", "support_ticket_update", "support_ticket_insert",
)


class Migration(migrations.Migration):
    dependencies = [("support", "0001_initial")]

    operations = [
        PostgreSQLRunSQL(FORWARD, REVERSE),
        MariaDBRunSQL(MARIADB_FORWARD, MARIADB_REVERSE),
    ]
