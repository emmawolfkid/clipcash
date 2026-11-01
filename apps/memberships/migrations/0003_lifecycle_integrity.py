"""
Database-enforced membership integrity (PostgreSQL).

* Subscriptions are inserted only as PENDING, with no activation data.
* Snapshot columns (member, plan, version, name, price, currency, duration,
  benefits, entitlements, pending deadline, renewal link) never change.
* Activation data (source, reference, amount, period, activator) is written
  once, in the PENDING -> ACTIVE transition, and never changed afterwards.
* Status follows the state machine:
      PENDING   -> ACTIVE | CANCELLED
      ACTIVE    -> SUSPENDED | EXPIRED | CANCELLED
      SUSPENDED -> ACTIVE | EXPIRED | CANCELLED
      EXPIRED, CANCELLED: terminal
* Subscriptions and plan versions cannot be deleted; plan versions are
  append-only; plans cannot be deleted, their code is immutable and ARCHIVED
  is terminal.

Limitation: the table owner or a superuser can disable these triggers.
"""

from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION membership_subscription_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'subscriptions cannot be deleted' USING ERRCODE = 'insufficient_privilege';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'PENDING' OR NEW.activated_at IS NOT NULL OR NEW.activation_source <> ''
           OR NEW.payment_reference <> '' OR NEW.amount_paid IS NOT NULL
           OR NEW.starts_at IS NOT NULL OR NEW.expires_at IS NOT NULL THEN
            RAISE EXCEPTION 'subscriptions must be created PENDING without activation data'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    -- UPDATE: snapshots are immutable.
    IF NEW.member_id <> OLD.member_id OR NEW.plan_id <> OLD.plan_id
       OR NEW.plan_version_id <> OLD.plan_version_id OR NEW.plan_name <> OLD.plan_name
       OR NEW.price_amount <> OLD.price_amount OR NEW.currency <> OLD.currency
       OR NEW.duration_days <> OLD.duration_days OR NEW.benefits::text <> OLD.benefits::text
       OR NEW.entitlements::text <> OLD.entitlements::text OR NEW.pending_until <> OLD.pending_until
       OR NEW.renewal_of_id IS DISTINCT FROM OLD.renewal_of_id OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'subscription snapshots are immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;

    -- Activation data: set only when leaving PENDING for ACTIVE, then frozen.
    IF NEW.activated_at IS DISTINCT FROM OLD.activated_at
       OR NEW.activation_source IS DISTINCT FROM OLD.activation_source
       OR NEW.payment_reference IS DISTINCT FROM OLD.payment_reference
       OR NEW.amount_paid IS DISTINCT FROM OLD.amount_paid
       OR NEW.starts_at IS DISTINCT FROM OLD.starts_at
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.activated_by_id IS DISTINCT FROM OLD.activated_by_id THEN
        IF NOT (OLD.status = 'PENDING' AND NEW.status = 'ACTIVE' AND OLD.activated_at IS NULL) THEN
            RAISE EXCEPTION 'activation data can only be set when a pending subscription is activated'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;

    IF NEW.status <> OLD.status AND NOT (
        (OLD.status = 'PENDING'   AND NEW.status IN ('ACTIVE', 'CANCELLED')) OR
        (OLD.status = 'ACTIVE'    AND NEW.status IN ('SUSPENDED', 'EXPIRED', 'CANCELLED')) OR
        (OLD.status = 'SUSPENDED' AND NEW.status IN ('ACTIVE', 'EXPIRED', 'CANCELLED'))
    ) THEN
        RAISE EXCEPTION 'invalid subscription transition % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER membership_subscription_guard
    BEFORE INSERT OR UPDATE OR DELETE ON memberships_subscription
    FOR EACH ROW EXECUTE FUNCTION membership_subscription_guard();

CREATE OR REPLACE FUNCTION membership_plan_version_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'plan versions are append-only' USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER membership_plan_version_immutable
    BEFORE UPDATE OR DELETE ON memberships_membershipplanversion
    FOR EACH ROW EXECUTE FUNCTION membership_plan_version_immutable();

CREATE OR REPLACE FUNCTION membership_plan_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'membership plans cannot be deleted; archive them' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.code <> OLD.code THEN
        RAISE EXCEPTION 'membership plan codes are immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.status = 'ARCHIVED' AND NEW.status <> 'ARCHIVED' THEN
        RAISE EXCEPTION 'archived plans cannot be reactivated' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER membership_plan_guard
    BEFORE UPDATE OR DELETE ON memberships_membershipplan
    FOR EACH ROW EXECUTE FUNCTION membership_plan_guard();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS membership_plan_guard ON memberships_membershipplan;
DROP FUNCTION IF EXISTS membership_plan_guard();
DROP TRIGGER IF EXISTS membership_plan_version_immutable ON memberships_membershipplanversion;
DROP FUNCTION IF EXISTS membership_plan_version_immutable();
DROP TRIGGER IF EXISTS membership_subscription_guard ON memberships_subscription;
DROP FUNCTION IF EXISTS membership_subscription_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("memberships", "0002_plans_and_subscriptions")]

    operations = [migrations.RunSQL(FORWARD, REVERSE)]
