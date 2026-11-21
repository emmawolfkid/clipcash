"""
Database-enforced watch-session integrity (PostgreSQL).

* Sessions are never deleted, and are created CREATED / PENDING / NONE with
  zero evidence and no counted slot.
* Member, campaign, approved revision, video, video length and the required
  watch time never change.
* Lifecycle: CREATED -> IN_PROGRESS | EXPIRED | INVALIDATED;
             IN_PROGRESS -> FINALIZED | INVALIDATED; closed states are terminal.
* Outcome:   PENDING -> any result; SUSPICIOUS_REVIEW -> VERIFIED_EVIDENCE | REJECTED;
             other results are final.
* Review:    NONE -> PENDING (when closing) -> CONFIRMED | REJECTED.
* Evidence only grows while open and is frozen once the session is closed.
  Together with the check constraints (VERIFIED needs credited and continuous
  coverage >= required), nobody, reviewers included, can fabricate evidence.

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
CREATE OR REPLACE FUNCTION viewing_session_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'watch sessions cannot be deleted' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'CREATED' OR NEW.outcome <> 'PENDING' OR NEW.review_status <> 'NONE'
           OR NEW.qualifying_slot IS NOT NULL OR NEW.credited_ms <> 0 OR NEW.coverage_ms <> 0
           OR NEW.event_count <> 0 OR NEW.last_sequence <> 0 OR NEW.started_at IS NOT NULL THEN
            RAISE EXCEPTION 'watch sessions must be created empty' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.member_id <> OLD.member_id OR NEW.campaign_id <> OLD.campaign_id
       OR NEW.campaign_revision <> OLD.campaign_revision OR NEW.video_id <> OLD.video_id
       OR NEW.video_duration_ms IS DISTINCT FROM OLD.video_duration_ms
       OR NEW.required_ms IS DISTINCT FROM OLD.required_ms OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'watch session identity cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;

    IF NEW.state <> OLD.state AND NOT (
        (OLD.state = 'CREATED' AND NEW.state IN ('IN_PROGRESS', 'EXPIRED', 'INVALIDATED'))
        OR (OLD.state = 'IN_PROGRESS' AND NEW.state IN ('FINALIZED', 'INVALIDATED'))
    ) THEN
        RAISE EXCEPTION 'invalid watch session state change % -> %', OLD.state, NEW.state
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.outcome <> OLD.outcome AND NOT (
        OLD.outcome = 'PENDING'
        OR (OLD.outcome = 'SUSPICIOUS_REVIEW' AND NEW.outcome IN ('VERIFIED_EVIDENCE', 'REJECTED'))
    ) THEN
        RAISE EXCEPTION 'invalid watch session outcome change % -> %', OLD.outcome, NEW.outcome
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.review_status <> OLD.review_status AND NOT (
        (OLD.review_status = 'NONE' AND NEW.review_status = 'PENDING' AND OLD.outcome = 'PENDING')
        OR (OLD.review_status = 'PENDING' AND NEW.review_status IN ('CONFIRMED', 'REJECTED'))
    ) THEN
        RAISE EXCEPTION 'invalid watch session review change % -> %', OLD.review_status, NEW.review_status
            USING ERRCODE = 'check_violation';
    END IF;

    IF OLD.state IN ('CREATED', 'IN_PROGRESS') THEN
        IF NEW.credited_ms < OLD.credited_ms OR NEW.coverage_ms < OLD.coverage_ms
           OR NEW.last_sequence < OLD.last_sequence OR NEW.event_count < OLD.event_count THEN
            RAISE EXCEPTION 'watch evidence only grows' USING ERRCODE = 'check_violation';
        END IF;
    ELSIF NEW.credited_ms <> OLD.credited_ms OR NEW.coverage_ms <> OLD.coverage_ms
          OR NEW.last_sequence <> OLD.last_sequence OR NEW.event_count <> OLD.event_count
          OR NEW.last_position_ms <> OLD.last_position_ms OR NEW.seek_count <> OLD.seek_count
          OR NEW.seek_forward_ms <> OLD.seek_forward_ms OR NEW.speed_anomaly_count <> OLD.speed_anomaly_count
          OR NEW.gap_count <> OLD.gap_count OR NEW.out_of_order_count <> OLD.out_of_order_count
          OR NEW.duplicate_event_count <> OLD.duplicate_event_count
          OR NEW.rejected_event_count <> OLD.rejected_event_count
          OR NEW.rate_rejection_count <> OLD.rate_rejection_count
          OR NEW.flags::text <> OLD.flags::text OR NEW.reasons::text <> OLD.reasons::text
          OR NEW.finalized_at IS DISTINCT FROM OLD.finalized_at THEN
        RAISE EXCEPTION 'evidence of a closed watch session cannot change' USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.qualifying_slot IS DISTINCT FROM OLD.qualifying_slot AND NOT (
        (OLD.qualifying_slot IS NULL AND OLD.outcome = 'PENDING')
        OR (NEW.qualifying_slot IS NULL AND NEW.outcome = 'REJECTED' AND OLD.outcome = 'SUSPICIOUS_REVIEW')
    ) THEN
        RAISE EXCEPTION 'a counted view cannot be reassigned' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER viewing_session_guard BEFORE INSERT OR UPDATE OR DELETE ON viewing_watchsession
    FOR EACH ROW EXECUTE FUNCTION viewing_session_guard();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS viewing_session_guard ON viewing_watchsession;
DROP FUNCTION IF EXISTS viewing_session_guard();
"""


# DB-01: the same rules on MariaDB (one trigger per event).
SESSION_IDENTITY = ("member_id", "campaign_id", "campaign_revision", "video_id", "video_duration_ms", "required_ms",
                    "created_at")
SESSION_EVIDENCE = ("credited_ms", "coverage_ms", "last_sequence", "event_count", "last_position_ms", "seek_count",
                    "seek_forward_ms", "speed_anomaly_count", "gap_count", "out_of_order_count",
                    "duplicate_event_count", "rejected_event_count", "rate_rejection_count", "flags", "reasons",
                    "finalized_at")

MARIADB_SESSION_UPDATE = trigger("viewing_session_update", "BEFORE UPDATE", "viewing_watchsession", f"""
    DECLARE message VARCHAR(200);
    IF {changed(*SESSION_IDENTITY)} THEN
        {privilege_error("watch session identity cannot change")}
    END IF;
    IF {changed("state")} AND NOT (
        (BINARY OLD.state = 'CREATED' AND BINARY NEW.state IN ('IN_PROGRESS', 'EXPIRED', 'INVALIDATED'))
        OR (BINARY OLD.state = 'IN_PROGRESS' AND BINARY NEW.state IN ('FINALIZED', 'INVALIDATED'))
    ) THEN
        SET message = CONCAT('invalid watch session state change ', OLD.state, ' -> ', NEW.state);
        SIGNAL SQLSTATE '23000' SET MYSQL_ERRNO = 4025, MESSAGE_TEXT = message;
    END IF;
    IF {changed("outcome")} AND NOT (
        BINARY OLD.outcome = 'PENDING'
        OR (BINARY OLD.outcome = 'SUSPICIOUS_REVIEW' AND BINARY NEW.outcome IN ('VERIFIED_EVIDENCE', 'REJECTED'))
    ) THEN
        SET message = CONCAT('invalid watch session outcome change ', OLD.outcome, ' -> ', NEW.outcome);
        SIGNAL SQLSTATE '23000' SET MYSQL_ERRNO = 4025, MESSAGE_TEXT = message;
    END IF;
    IF {changed("review_status")} AND NOT (
        (BINARY OLD.review_status = 'NONE' AND BINARY NEW.review_status = 'PENDING' AND BINARY OLD.outcome = 'PENDING')
        OR (BINARY OLD.review_status = 'PENDING' AND BINARY NEW.review_status IN ('CONFIRMED', 'REJECTED'))
    ) THEN
        SET message = CONCAT('invalid watch session review change ', OLD.review_status, ' -> ', NEW.review_status);
        SIGNAL SQLSTATE '23000' SET MYSQL_ERRNO = 4025, MESSAGE_TEXT = message;
    END IF;
    IF BINARY OLD.state IN ('CREATED', 'IN_PROGRESS') THEN
        IF NEW.credited_ms < OLD.credited_ms OR NEW.coverage_ms < OLD.coverage_ms
           OR NEW.last_sequence < OLD.last_sequence OR NEW.event_count < OLD.event_count THEN
            {check_error("watch evidence only grows")}
        END IF;
    ELSEIF {changed(*SESSION_EVIDENCE)} THEN
        {check_error("evidence of a closed watch session cannot change")}
    END IF;
    IF {changed("qualifying_slot")} AND NOT (
        (OLD.qualifying_slot IS NULL AND BINARY OLD.outcome = 'PENDING')
        OR (NEW.qualifying_slot IS NULL AND BINARY NEW.outcome = 'REJECTED'
            AND BINARY OLD.outcome IN ('SUSPICIOUS_REVIEW'))
    ) THEN
        {check_error("a counted view cannot be reassigned")}
    END IF;""")

MARIADB_FORWARD = [
    trigger("viewing_session_insert", "BEFORE INSERT", "viewing_watchsession", f"""
    IF BINARY NEW.state <> 'CREATED' OR BINARY NEW.outcome <> 'PENDING' OR BINARY NEW.review_status <> 'NONE'
       OR NEW.qualifying_slot IS NOT NULL OR NEW.credited_ms <> 0 OR NEW.coverage_ms <> 0
       OR NEW.event_count <> 0 OR NEW.last_sequence <> 0 OR NEW.started_at IS NOT NULL THEN
        {check_error("watch sessions must be created empty")}
    END IF;"""),
    MARIADB_SESSION_UPDATE,
    trigger("viewing_session_delete", "BEFORE DELETE", "viewing_watchsession",
            privilege_error("watch sessions cannot be deleted")),
]

MARIADB_REVERSE = drop_triggers("viewing_session_delete", "viewing_session_update", "viewing_session_insert")


class Migration(migrations.Migration):
    dependencies = [("viewing", "0001_initial")]

    operations = [
        PostgreSQLRunSQL(FORWARD, REVERSE),
        MariaDBRunSQL(MARIADB_FORWARD, MARIADB_REVERSE),
    ]
