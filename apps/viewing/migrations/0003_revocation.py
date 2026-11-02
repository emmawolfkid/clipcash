"""
Staff revocation of verified views (Phase 07 integration).

A finalized VERIFIED_EVIDENCE session may later be revoked by a reviewer
(outcome -> REJECTED, review -> REJECTED, counted slot released), e.g. when
fraud is found after the fact. Its demo reward is reversed in the same
transaction (apps.rewards). Evidence stays frozen; nothing else changes.
Replaces the viewing_session_guard() function body; the trigger is unchanged.
"""

from django.db import migrations

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
        OR (OLD.outcome = 'VERIFIED_EVIDENCE' AND NEW.outcome = 'REJECTED')
    ) THEN
        RAISE EXCEPTION 'invalid watch session outcome change % -> %', OLD.outcome, NEW.outcome
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.review_status <> OLD.review_status AND NOT (
        (OLD.review_status = 'NONE' AND NEW.review_status = 'PENDING' AND OLD.outcome = 'PENDING')
        OR (OLD.review_status = 'PENDING' AND NEW.review_status IN ('CONFIRMED', 'REJECTED'))
        OR (OLD.review_status IN ('NONE', 'CONFIRMED') AND NEW.review_status = 'REJECTED'
            AND OLD.outcome = 'VERIFIED_EVIDENCE' AND NEW.outcome = 'REJECTED')
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
        OR (NEW.qualifying_slot IS NULL AND NEW.outcome = 'REJECTED'
            AND OLD.outcome IN ('SUSPICIOUS_REVIEW', 'VERIFIED_EVIDENCE'))
    ) THEN
        RAISE EXCEPTION 'a counted view cannot be reassigned' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
"""

# The Phase 06 (0002) function body, restored on reverse.
REVERSE = r"""

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
"""


class Migration(migrations.Migration):
    dependencies = [("viewing", "0002_integrity")]

    operations = [migrations.RunSQL(FORWARD, REVERSE)]
