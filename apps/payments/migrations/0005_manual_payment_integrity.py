"""
PAY-01: database-enforced integrity for manual (Airtel Lipa) membership payments.

* Neither table is ever deleted from (the existing payments_no_delete()).
* Payment instructions are append-only history: created active; the only change
  allowed is retiring an active row (is_active true -> false with who and when).
  Business number, recipient name and text never change in place.
* Payment claims are created PENDING_VERIFICATION without review data. What the
  member submitted (member, membership, plan, amount, phone, transaction reference,
  instruction shown, stated payment date) never changes. Status follows
      PENDING_VERIFICATION -> APPROVED | REJECTED | CANCELLED
  and APPROVED, REJECTED and CANCELLED are final: such rows cannot change at all.
  The model's check and unique constraints (migration 0004) keep approvals
  complete, a transaction reference single-use and one approval per membership.

New functions pin their search_path (RG-03) and are listed in FUNCTIONS, which
apps.core.tests.test_database_hardening reads. Limitation: the table owner or a
superuser can disable these triggers.
"""

from django.db import migrations

SEARCH_PATH = "pg_catalog, public, pg_temp"
FUNCTIONS = ("payments_manual_claim_guard()", "payments_manual_instruction_guard()")

FORWARD = rf"""
CREATE OR REPLACE FUNCTION payments_manual_instruction_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NOT NEW.is_active OR NEW.retired_at IS NOT NULL OR NEW.retired_by_id IS NOT NULL THEN
            RAISE EXCEPTION 'payment instructions are created active' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NOT OLD.is_active THEN
        RAISE EXCEPTION 'retired payment instructions cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.method <> OLD.method OR NEW.business_number <> OLD.business_number
       OR NEW.recipient_name <> OLD.recipient_name OR NEW.instructions <> OLD.instructions
       OR NEW.created_by_id <> OLD.created_by_id OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'payment instructions are append-only; publish new details instead'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payments_manual_instruction_guard
BEFORE INSERT OR UPDATE ON payments_manualpaymentinstruction
FOR EACH ROW EXECUTE FUNCTION payments_manual_instruction_guard();

CREATE TRIGGER payments_manual_instruction_no_delete
BEFORE DELETE ON payments_manualpaymentinstruction
FOR EACH ROW EXECUTE FUNCTION payments_no_delete();

CREATE OR REPLACE FUNCTION payments_manual_claim_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'PENDING_VERIFICATION' OR NEW.reviewed_by_id IS NOT NULL OR NEW.reviewed_at IS NOT NULL
           OR NEW.verified_amount IS NOT NULL OR NEW.verified_reference_key <> '' OR NEW.receipt_confirmed
           OR NEW.rejection_reason <> '' OR NEW.ledger_entry_id IS NOT NULL OR NEW.cancelled_at IS NOT NULL THEN
            RAISE EXCEPTION 'payment claims must be created pending, without review data'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status <> 'PENDING_VERIFICATION' THEN
        RAISE EXCEPTION 'a % payment claim is final', lower(OLD.status) USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.reference <> OLD.reference OR NEW.method <> OLD.method OR NEW.member_id <> OLD.member_id
       OR NEW.subscription_id <> OLD.subscription_id OR NEW.instruction_id <> OLD.instruction_id
       OR NEW.plan_name <> OLD.plan_name OR NEW.amount <> OLD.amount OR NEW.currency <> OLD.currency
       OR NEW.payer_phone <> OLD.payer_phone OR NEW.transaction_reference <> OLD.transaction_reference
       OR NEW.reference_key <> OLD.reference_key OR NEW.paid_on IS DISTINCT FROM OLD.paid_on
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'submitted payment details cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status NOT IN ('PENDING_VERIFICATION', 'APPROVED', 'REJECTED', 'CANCELLED') THEN
        RAISE EXCEPTION 'invalid payment claim transition % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payments_manual_claim_guard
BEFORE INSERT OR UPDATE ON payments_manualmembershippayment
FOR EACH ROW EXECUTE FUNCTION payments_manual_claim_guard();

CREATE TRIGGER payments_manual_claim_no_delete
BEFORE DELETE ON payments_manualmembershippayment
FOR EACH ROW EXECUTE FUNCTION payments_no_delete();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS payments_manual_claim_no_delete ON payments_manualmembershippayment;
DROP TRIGGER IF EXISTS payments_manual_claim_guard ON payments_manualmembershippayment;
DROP FUNCTION IF EXISTS payments_manual_claim_guard();
DROP TRIGGER IF EXISTS payments_manual_instruction_no_delete ON payments_manualpaymentinstruction;
DROP TRIGGER IF EXISTS payments_manual_instruction_guard ON payments_manualpaymentinstruction;
DROP FUNCTION IF EXISTS payments_manual_instruction_guard();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0004_manual_membership_payments"),
    ]

    operations = [migrations.RunSQL(FORWARD, REVERSE)]
