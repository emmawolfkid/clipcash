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


class Migration(migrations.Migration):
    dependencies = [("support", "0001_initial")]

    operations = [migrations.RunSQL(FORWARD, REVERSE)]
