"""
Database-enforced advertising integrity (PostgreSQL).

* Organizations, team memberships, invitations, video grants and campaigns are
  never deleted (deactivate, revoke, cancel or archive instead).
* Organizations are created PENDING; verification follows
      PENDING   -> APPROVED | REJECTED
      APPROVED  -> SUSPENDED
      SUSPENDED -> APPROVED
      REJECTED  -> PENDING (resubmitted)
* Campaigns are created DRAFT and never change organization; status follows
      DRAFT     -> SUBMITTED | CANCELLED
      SUBMITTED -> DRAFT | SCHEDULED | REJECTED | CANCELLED
      REJECTED  -> DRAFT | CANCELLED | ARCHIVED
      SCHEDULED -> ACTIVE | PAUSED | COMPLETED | CANCELLED | DRAFT
      ACTIVE    -> PAUSED | COMPLETED | CANCELLED
      PAUSED    -> ACTIVE | SCHEDULED | COMPLETED | CANCELLED | DRAFT
      COMPLETED, CANCELLED -> ARCHIVED; ARCHIVED is terminal
* Outside DRAFT/REJECTED, the reviewed columns cannot change without a new
  revision (and the approved-revision check constraint then forbids running it).
  Targeted membership plans (many-to-many) are protected by the services.

Limitation: the table owner or a superuser can disable these triggers.
"""

from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION advertising_no_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows cannot be deleted', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE OR REPLACE FUNCTION advertising_organization_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.verification_status <> 'PENDING' OR NEW.reviewed_at IS NOT NULL THEN
            RAISE EXCEPTION 'advertisers must be created PENDING' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.verification_status <> OLD.verification_status AND NOT (
        (OLD.verification_status = 'PENDING' AND NEW.verification_status IN ('APPROVED', 'REJECTED'))
        OR (OLD.verification_status = 'APPROVED' AND NEW.verification_status = 'SUSPENDED')
        OR (OLD.verification_status = 'SUSPENDED' AND NEW.verification_status = 'APPROVED')
        OR (OLD.verification_status = 'REJECTED' AND NEW.verification_status = 'PENDING')
    ) THEN
        RAISE EXCEPTION 'invalid advertiser verification change % -> %', OLD.verification_status,
            NEW.verification_status USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION advertising_campaign_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'DRAFT' OR NEW.approved_revision IS NOT NULL OR NEW.funding_status <> 'UNFUNDED' THEN
            RAISE EXCEPTION 'campaigns must be created as unfunded drafts' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.organization_id <> OLD.organization_id THEN
        RAISE EXCEPTION 'a campaign cannot move to another advertiser' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.revision < OLD.revision THEN
        RAISE EXCEPTION 'campaign revisions only increase' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status <> OLD.status AND NOT (
        (OLD.status = 'DRAFT' AND NEW.status IN ('SUBMITTED', 'CANCELLED'))
        OR (OLD.status = 'SUBMITTED' AND NEW.status IN ('DRAFT', 'SCHEDULED', 'REJECTED', 'CANCELLED'))
        OR (OLD.status = 'REJECTED' AND NEW.status IN ('DRAFT', 'CANCELLED', 'ARCHIVED'))
        OR (OLD.status = 'SCHEDULED' AND NEW.status IN ('ACTIVE', 'PAUSED', 'COMPLETED', 'CANCELLED', 'DRAFT'))
        OR (OLD.status = 'ACTIVE' AND NEW.status IN ('PAUSED', 'COMPLETED', 'CANCELLED'))
        OR (OLD.status = 'PAUSED' AND NEW.status IN ('ACTIVE', 'SCHEDULED', 'COMPLETED', 'CANCELLED', 'DRAFT'))
        OR (OLD.status IN ('COMPLETED', 'CANCELLED') AND NEW.status = 'ARCHIVED')
    ) THEN
        RAISE EXCEPTION 'invalid campaign status change % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status NOT IN ('DRAFT', 'REJECTED') AND NEW.revision = OLD.revision AND (
        NEW.name IS DISTINCT FROM OLD.name OR NEW.description IS DISTINCT FROM OLD.description
        OR NEW.video_id IS DISTINCT FROM OLD.video_id OR NEW.starts_at IS DISTINCT FROM OLD.starts_at
        OR NEW.ends_at IS DISTINCT FROM OLD.ends_at OR NEW.kind IS DISTINCT FROM OLD.kind
        OR NEW.max_views_per_member IS DISTINCT FROM OLD.max_views_per_member
        OR NEW.demo_budget_amount IS DISTINCT FROM OLD.demo_budget_amount
        OR NEW.demo_reward_per_view IS DISTINCT FROM OLD.demo_reward_per_view
    ) THEN
        RAISE EXCEPTION 'reviewed campaign details cannot change without a new revision'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER advertising_organization_guard BEFORE INSERT OR UPDATE ON advertising_organization
    FOR EACH ROW EXECUTE FUNCTION advertising_organization_guard();
CREATE TRIGGER advertising_campaign_guard BEFORE INSERT OR UPDATE ON advertising_campaign
    FOR EACH ROW EXECUTE FUNCTION advertising_campaign_guard();
CREATE TRIGGER advertising_organization_no_delete BEFORE DELETE ON advertising_organization
    FOR EACH ROW EXECUTE FUNCTION advertising_no_delete();
CREATE TRIGGER advertising_membership_no_delete BEFORE DELETE ON advertising_advertisermembership
    FOR EACH ROW EXECUTE FUNCTION advertising_no_delete();
CREATE TRIGGER advertising_invitation_no_delete BEFORE DELETE ON advertising_advertiserinvitation
    FOR EACH ROW EXECUTE FUNCTION advertising_no_delete();
CREATE TRIGGER advertising_video_grant_no_delete BEFORE DELETE ON advertising_advertiservideo
    FOR EACH ROW EXECUTE FUNCTION advertising_no_delete();
CREATE TRIGGER advertising_campaign_no_delete BEFORE DELETE ON advertising_campaign
    FOR EACH ROW EXECUTE FUNCTION advertising_no_delete();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS advertising_campaign_no_delete ON advertising_campaign;
DROP TRIGGER IF EXISTS advertising_video_grant_no_delete ON advertising_advertiservideo;
DROP TRIGGER IF EXISTS advertising_invitation_no_delete ON advertising_advertiserinvitation;
DROP TRIGGER IF EXISTS advertising_membership_no_delete ON advertising_advertisermembership;
DROP TRIGGER IF EXISTS advertising_organization_no_delete ON advertising_organization;
DROP TRIGGER IF EXISTS advertising_campaign_guard ON advertising_campaign;
DROP TRIGGER IF EXISTS advertising_organization_guard ON advertising_organization;
DROP FUNCTION IF EXISTS advertising_campaign_guard();
DROP FUNCTION IF EXISTS advertising_organization_guard();
DROP FUNCTION IF EXISTS advertising_no_delete();
"""


class Migration(migrations.Migration):
    dependencies = [("advertising", "0001_initial")]

    operations = [migrations.RunSQL(FORWARD, REVERSE)]
