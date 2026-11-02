"""
Withdrawal integrity (PostgreSQL), DEMONSTRATION ONLY.

* REAL withdrawal requests are locked: every insert, update and delete on
  withdrawals_withdrawalrequest is refused. Activating real withdrawals needs a
  reviewed migration (together with lifting the Phase 09 wallet posting lock).
* Beneficiaries: destination details never change (a change is a new
  beneficiary); ACTIVE -> DEACTIVATED and UNVERIFIED -> DEMO_VERIFIED only.
* Demo withdrawals: member, destination snapshot, amount, reference, author,
  idempotency key and risk assessment never change; approvers, payout initiator
  and payout reference are written once; status follows
      DEMO_REQUESTED    -> DEMO_UNDER_REVIEW | DEMO_REJECTED | DEMO_CANCELLED | DEMO_EXPIRED
      DEMO_UNDER_REVIEW -> DEMO_APPROVED | DEMO_REJECTED | DEMO_CANCELLED | DEMO_EXPIRED
      DEMO_APPROVED     -> DEMO_PAYOUT_PENDING | DEMO_REJECTED | DEMO_CANCELLED
      DEMO_PAYOUT_PENDING -> DEMO_UNCERTAIN | DEMO_PAID | DEMO_FAILED
      DEMO_UNCERTAIN    -> DEMO_PAID | DEMO_FAILED
      DEMO_PAID         -> DEMO_RETURNED;  everything else is terminal.
* Decision history is append-only; payout events cannot be deleted; nothing
  else is deleted either.

Limitation: the table owner or a superuser can disable these triggers.
"""

from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION withdrawals_locked() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'real withdrawals are not active in this phase' USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE OR REPLACE FUNCTION withdrawals_no_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows cannot be deleted', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE OR REPLACE FUNCTION withdrawals_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE OR REPLACE FUNCTION withdrawals_beneficiary_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'ACTIVE' OR NEW.verification <> 'UNVERIFIED' THEN
            RAISE EXCEPTION 'beneficiaries are created active and unverified' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.member_id <> OLD.member_id OR NEW.kind <> OLD.kind OR NEW.network <> OLD.network
       OR NEW.bank_code <> OLD.bank_code OR NEW.holder_name <> OLD.holder_name
       OR NEW.identifier_last4 <> OLD.identifier_last4
       OR NEW.identifier_fingerprint <> OLD.identifier_fingerprint OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'payout destination details cannot change; add a new destination'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status <> OLD.status AND NOT (OLD.status = 'ACTIVE' AND NEW.status = 'DEACTIVATED') THEN
        RAISE EXCEPTION 'invalid beneficiary status change' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.verification <> OLD.verification
       AND NOT (OLD.verification = 'UNVERIFIED' AND NEW.verification = 'DEMO_VERIFIED') THEN
        RAISE EXCEPTION 'invalid beneficiary verification change' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION withdrawals_demo_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'DEMO_REQUESTED' OR NEW.approved_by_id IS NOT NULL OR NEW.payout_reference <> '' THEN
            RAISE EXCEPTION 'demo withdrawals are created as new requests' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.reference <> OLD.reference OR NEW.member_id <> OLD.member_id OR NEW.beneficiary_id <> OLD.beneficiary_id
       OR NEW.destination_kind <> OLD.destination_kind OR NEW.destination_provider <> OLD.destination_provider
       OR NEW.destination_last4 <> OLD.destination_last4
       OR NEW.destination_fingerprint <> OLD.destination_fingerprint
       OR NEW.destination_holder <> OLD.destination_holder OR NEW.simulated_amount <> OLD.simulated_amount
       OR NEW.currency <> OLD.currency OR NEW.created_by_id <> OLD.created_by_id
       OR NEW.idempotency_key <> OLD.idempotency_key OR NEW.high_risk <> OLD.high_risk
       OR NEW.flags::text <> OLD.flags::text OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'demo withdrawal terms cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF (OLD.approved_by_id IS NOT NULL AND NEW.approved_by_id IS DISTINCT FROM OLD.approved_by_id)
       OR (OLD.second_approved_by_id IS NOT NULL AND NEW.second_approved_by_id IS DISTINCT FROM OLD.second_approved_by_id)
       OR (OLD.payout_initiated_by_id IS NOT NULL
           AND NEW.payout_initiated_by_id IS DISTINCT FROM OLD.payout_initiated_by_id)
       OR (OLD.payout_reference <> '' AND NEW.payout_reference <> OLD.payout_reference) THEN
        RAISE EXCEPTION 'approvals and payout references are written once' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status <> OLD.status AND NOT (
        (OLD.status = 'DEMO_REQUESTED' AND NEW.status IN ('DEMO_UNDER_REVIEW', 'DEMO_REJECTED', 'DEMO_CANCELLED',
                                                          'DEMO_EXPIRED'))
        OR (OLD.status = 'DEMO_UNDER_REVIEW' AND NEW.status IN ('DEMO_APPROVED', 'DEMO_REJECTED', 'DEMO_CANCELLED',
                                                                'DEMO_EXPIRED'))
        OR (OLD.status = 'DEMO_APPROVED' AND NEW.status IN ('DEMO_PAYOUT_PENDING', 'DEMO_REJECTED', 'DEMO_CANCELLED'))
        OR (OLD.status = 'DEMO_PAYOUT_PENDING' AND NEW.status IN ('DEMO_UNCERTAIN', 'DEMO_PAID', 'DEMO_FAILED'))
        OR (OLD.status = 'DEMO_UNCERTAIN' AND NEW.status IN ('DEMO_PAID', 'DEMO_FAILED'))
        OR (OLD.status = 'DEMO_PAID' AND NEW.status = 'DEMO_RETURNED')
    ) THEN
        RAISE EXCEPTION 'invalid demo withdrawal status change % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION withdrawals_exception_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'RESOLVED' AND NEW.status <> OLD.status THEN
        RAISE EXCEPTION 'resolved exceptions are final' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER withdrawals_request_locked BEFORE INSERT OR UPDATE OR DELETE ON withdrawals_withdrawalrequest
    FOR EACH ROW EXECUTE FUNCTION withdrawals_locked();
CREATE TRIGGER withdrawals_beneficiary_guard BEFORE INSERT OR UPDATE ON withdrawals_payoutbeneficiary
    FOR EACH ROW EXECUTE FUNCTION withdrawals_beneficiary_guard();
CREATE TRIGGER withdrawals_demo_guard BEFORE INSERT OR UPDATE ON withdrawals_demowithdrawal
    FOR EACH ROW EXECUTE FUNCTION withdrawals_demo_guard();
CREATE TRIGGER withdrawals_exception_guard BEFORE UPDATE ON withdrawals_withdrawalexception
    FOR EACH ROW EXECUTE FUNCTION withdrawals_exception_guard();
CREATE TRIGGER withdrawals_decision_append_only BEFORE UPDATE OR DELETE ON withdrawals_demowithdrawaldecision
    FOR EACH ROW EXECUTE FUNCTION withdrawals_append_only();
CREATE TRIGGER withdrawals_event_no_delete BEFORE DELETE ON withdrawals_payoutevent
    FOR EACH ROW EXECUTE FUNCTION withdrawals_no_delete();
CREATE TRIGGER withdrawals_beneficiary_no_delete BEFORE DELETE ON withdrawals_payoutbeneficiary
    FOR EACH ROW EXECUTE FUNCTION withdrawals_no_delete();
CREATE TRIGGER withdrawals_demo_no_delete BEFORE DELETE ON withdrawals_demowithdrawal
    FOR EACH ROW EXECUTE FUNCTION withdrawals_no_delete();
CREATE TRIGGER withdrawals_exception_no_delete BEFORE DELETE ON withdrawals_withdrawalexception
    FOR EACH ROW EXECUTE FUNCTION withdrawals_no_delete();
CREATE TRIGGER withdrawals_payout_no_delete BEFORE DELETE ON withdrawals_sandboxpayout
    FOR EACH ROW EXECUTE FUNCTION withdrawals_no_delete();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS withdrawals_payout_no_delete ON withdrawals_sandboxpayout;
DROP TRIGGER IF EXISTS withdrawals_exception_no_delete ON withdrawals_withdrawalexception;
DROP TRIGGER IF EXISTS withdrawals_demo_no_delete ON withdrawals_demowithdrawal;
DROP TRIGGER IF EXISTS withdrawals_beneficiary_no_delete ON withdrawals_payoutbeneficiary;
DROP TRIGGER IF EXISTS withdrawals_event_no_delete ON withdrawals_payoutevent;
DROP TRIGGER IF EXISTS withdrawals_decision_append_only ON withdrawals_demowithdrawaldecision;
DROP TRIGGER IF EXISTS withdrawals_exception_guard ON withdrawals_withdrawalexception;
DROP TRIGGER IF EXISTS withdrawals_demo_guard ON withdrawals_demowithdrawal;
DROP TRIGGER IF EXISTS withdrawals_beneficiary_guard ON withdrawals_payoutbeneficiary;
DROP TRIGGER IF EXISTS withdrawals_request_locked ON withdrawals_withdrawalrequest;
DROP FUNCTION IF EXISTS withdrawals_exception_guard();
DROP FUNCTION IF EXISTS withdrawals_demo_guard();
DROP FUNCTION IF EXISTS withdrawals_beneficiary_guard();
DROP FUNCTION IF EXISTS withdrawals_append_only();
DROP FUNCTION IF EXISTS withdrawals_no_delete();
DROP FUNCTION IF EXISTS withdrawals_locked();
"""


class Migration(migrations.Migration):
    dependencies = [("withdrawals", "0001_initial")]

    operations = [migrations.RunSQL(FORWARD, REVERSE)]
