"""
Advertising services: the only supported way to change advertisers and campaigns.

Rules enforced here (and, where possible, by PostgreSQL, see migration 0002):

* Tenant isolation: advertiser users act only on organizations they belong
  to (active AdvertiserMembership); lookups outside that scope are "not found".
* Organization membership never grants platform privileges, and staff
  accounts cannot join organizations (no self-approval).
* Every status change goes through one service function holding a row lock;
  reviewer decisions carry the revision/status the reviewer saw, so stale or
  conflicting decisions are rejected.
* Financial firewall: demo budgets are configuration, never money. Nothing
  here posts to the ledger, and campaign funding cannot be verified before
  Phase 08 (also locked by a database constraint).
"""

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.audit.services import DENIED, record_business_event, record_security_event
from apps.core import features
from apps.core.money import to_amount
from apps.memberships.access import check_access
from apps.memberships.models import MembershipPlan
from apps.memberships.services import member_eligibility
from apps.videos.models import Video
from apps.videos.providers import ProviderError, demo_allowed, get_provider

from .models import AdvertiserInvitation, AdvertiserMembership, AdvertiserVideo, Campaign, Organization

S = Campaign.Status
V = Organization.Verification
ROLE = AdvertiserMembership.Role

INVITATION_DAYS = 7
MAX_CAMPAIGN_DAYS = 366
MIN_REASON_LENGTH = 5
ORG_FIELDS = ("display_name", "legal_name", "contact_email", "contact_phone", "website", "country",
              "registration_number", "tax_id")
# Identity details are fixed once approved (changes need a new review via support).
ORG_IDENTITY_FIELDS = ("legal_name", "country", "registration_number", "tax_id")
CAMPAIGN_FIELDS = ("name", "description", "video", "starts_at", "ends_at", "max_views_per_member",
                   "demo_budget_amount", "demo_reward_per_view")
APPROVED_STATES = (S.SCHEDULED, S.ACTIVE, S.PAUSED)


class AdvertisingError(Exception):
    code = "advertising_error"

    def __init__(self, message, *, code=None):
        super().__init__(message)
        if code:
            self.code = code


class NotAuthorized(AdvertisingError):
    code = "not_authorized"


class NotFound(AdvertisingError):
    code = "not_found"


class InvalidState(AdvertisingError):
    code = "invalid_state"


class StaleDecision(AdvertisingError):
    code = "stale"


class FundingLocked(AdvertisingError):
    code = "funding_locked"


# --- Permissions and tenant scope -------------------------------------------------------


def _staff_with(user, perm):
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.is_active
                and user.is_staff and user.has_perm(perm))


def can_review_advertisers(user):
    return _staff_with(user, "advertising.review_advertisers")


def can_review_campaigns(user):
    return _staff_with(user, "advertising.review_campaigns")


def can_manage_advertiser_videos(user):
    return _staff_with(user, "advertising.manage_advertiser_videos")


def can_use_advertising_console(user):
    return can_review_advertisers(user) or can_review_campaigns(user) or can_manage_advertiser_videos(user)


def advertiser_account_reason(user):
    """"ok", or why this account cannot act as an advertiser."""
    eligible, reason = member_eligibility(user)
    if not eligible:
        return reason
    if user.is_staff:
        return "staff_account"  # Reviewers never act for an advertiser.
    return "ok"


def _deny(action, actor, target, reason):
    error = NotAuthorized("You are not allowed to do this.", code=reason)
    record_security_event("advertiser.action.denied", outcome=DENIED,
                          actor=actor if getattr(actor, "is_authenticated", False) else None,
                          target=target, metadata={"action": action, "reason": reason})
    raise error


def _require_staff(check, actor, action, target=None):
    if not check(actor):
        _deny(action, actor, target, "permission_missing")


def organizations_for(user):
    """Organizations where ``user`` is an active team member (empty for staff and inactive accounts)."""
    if advertiser_account_reason(user) != "ok":
        return Organization.objects.none()
    return Organization.objects.filter(memberships__user=user, memberships__is_active=True).distinct()


def membership_for(user, organization):
    if advertiser_account_reason(user) != "ok":
        return None
    return AdvertiserMembership.objects.filter(organization=organization, user=user, is_active=True).first()


def organization_for(user, organization_id):
    """The organization if ``user`` belongs to it; NotFound otherwise (no existence oracle)."""
    organization = organizations_for(user).filter(pk=organization_id).first()
    if organization is None:
        raise NotFound("Advertiser not found.")
    return organization


def campaign_for(user, organization, campaign_id):
    campaign = (Campaign.objects.select_related("organization", "video", "video__category")
                .filter(pk=campaign_id, organization=organization).first())
    if campaign is None:
        raise NotFound("Campaign not found.")
    return campaign


def _require_member(user, organization, action, *, owner=False):
    reason = advertiser_account_reason(user)
    if reason != "ok":
        _deny(action, user, organization, reason)
    membership = membership_for(user, organization)
    if membership is None:
        _deny(action, user, organization, "not_a_member")
    if owner and membership.role != ROLE.OWNER:
        _deny(action, user, organization, "owner_required")
    return membership


def _reason(text, *, required):
    text = (text or "").strip()
    if required and len(text) < MIN_REASON_LENGTH:
        raise ValidationError({"reason": "Please give a reason (at least 5 characters)."})
    return text[:300]


# --- Organizations --------------------------------------------------------------------------


def _clean_org_values(values):
    values = {k: (v.strip() if isinstance(v, str) else v) for k, v in values.items() if k in ORG_FIELDS}
    if "country" in values:
        values["country"] = (values["country"] or "").upper()
    if "contact_email" in values:
        values["contact_email"] = (values["contact_email"] or "").lower()
    return values


def create_organization(user, **values):
    """Advertiser onboarding: creates a PENDING organization with ``user`` as OWNER."""
    if not features.is_enabled("advertisers"):
        raise InvalidState("Advertiser accounts are not available at the moment.", code="feature_disabled")
    reason = advertiser_account_reason(user)
    if reason != "ok":
        _deny("organization.create", user, None, reason)
    values = _clean_org_values(values)
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)  # Serialize per user.
        if Organization.objects.filter(memberships__user=user, memberships__is_active=True,
                                       verification_status=V.PENDING).exists():
            raise InvalidState("You already have an advertiser application awaiting review.", code="pending_exists")
        organization = Organization(created_by=user, submitted_at=timezone.now(), **values)
        organization.full_clean()
        organization.save()
        AdvertiserMembership.objects.create(organization=organization, user=user, role=ROLE.OWNER)
        record_business_event("advertiser.created", actor=user, target=organization,
                              metadata={"country": organization.country})
    return organization


def update_organization(user, organization, changes):
    _require_member(user, organization, "organization.update", owner=True)
    changes = _clean_org_values(changes)
    with transaction.atomic():
        organization = Organization.objects.select_for_update().get(pk=organization.pk)
        if organization.verification_status == V.SUSPENDED:
            raise InvalidState("A suspended advertiser cannot be changed. Please contact support.")
        if organization.is_approved:
            for field in ORG_IDENTITY_FIELDS:
                if field in changes and changes[field] != getattr(organization, field):
                    raise ValidationError({field: "Verified identity details cannot be changed here. "
                                                  "Please contact support."})
        before = {k: getattr(organization, k) for k in changes}
        for key, value in changes.items():
            setattr(organization, key, value)
        organization.full_clean()
        changed = sorted(k for k in changes if getattr(organization, k) != before[k])
        if changed:
            organization.save()
            record_business_event("advertiser.updated", actor=user, target=organization, metadata={"fields": changed})
    return organization


def resubmit_organization(user, organization):
    _require_member(user, organization, "organization.resubmit", owner=True)
    with transaction.atomic():
        organization = Organization.objects.select_for_update().get(pk=organization.pk)
        if organization.verification_status != V.REJECTED:
            raise InvalidState("Only rejected applications can be resubmitted.")
        organization.verification_status = V.PENDING
        organization.submitted_at = timezone.now()
        organization.reviewed_at = None
        organization.reviewed_by = None
        organization.save()
        record_business_event("advertiser.resubmitted", actor=user, target=organization)
    return organization


ORG_DECISIONS = {
    # decision: (allowed from, to, reason required, audit name)
    "approve": ((V.PENDING,), V.APPROVED, False, "approved"),
    "reject": ((V.PENDING,), V.REJECTED, True, "rejected"),
    "suspend": ((V.APPROVED,), V.SUSPENDED, True, "suspended"),
    "reinstate": ((V.SUSPENDED,), V.APPROVED, False, "reinstated"),
}


def review_organization(actor, organization, decision, *, expected_status, reason=""):
    """Staff verification decision. ``expected_status`` is what the reviewer saw."""
    if decision not in ORG_DECISIONS:
        raise InvalidState("Unknown decision.")
    _require_staff(can_review_advertisers, actor, f"organization.{decision}", organization)
    allowed_from, target, reason_required, name = ORG_DECISIONS[decision]
    reason = _reason(reason, required=reason_required)
    if organization.memberships.filter(user=actor, is_active=True).exists():
        _deny(f"organization.{decision}", actor, organization, "self_review")
    with transaction.atomic():
        organization = Organization.objects.select_for_update().get(pk=organization.pk)
        current = organization.verification_status
        if current == target and expected_status == current:
            return organization  # Repeated submit of the same decision: no-op.
        if current != expected_status:
            raise StaleDecision("This advertiser changed since you opened it. Review it again.")
        if current not in allowed_from:
            raise InvalidState(f"An advertiser that is {organization.get_verification_status_display().lower()} "
                               f"cannot be {name}.")
        organization.verification_status = target
        organization.reviewed_at = timezone.now()
        organization.reviewed_by = actor
        organization.review_reason = reason
        organization.save()
        record_business_event(f"advertiser.verification.{name}", actor=actor, target=organization,
                              metadata={"from_status": current, "reason": reason[:100]})
    return organization


# --- Team --------------------------------------------------------------------------------


def invite_member(user, organization, email, role=ROLE.MANAGER):
    _require_member(user, organization, "member.invite", owner=True)
    if role not in ROLE.values:
        raise ValidationError({"role": "Choose a valid role."})
    email = (email or "").strip().lower()
    invitation = AdvertiserInvitation(organization=organization, email=email, role=role, invited_by=user,
                                      expires_at=timezone.now() + timedelta(days=INVITATION_DAYS))
    invitation.full_clean(exclude=["organization", "invited_by"])
    with transaction.atomic():
        organization = Organization.objects.select_for_update().get(pk=organization.pk)
        if organization.verification_status in (V.SUSPENDED, V.REJECTED):
            raise InvalidState("Invitations are not possible while the advertiser is not in good standing.")
        if organization.memberships.filter(is_active=True, user__email__iexact=email).exists():
            raise ValidationError({"email": "This person is already on the team."})
        organization.invitations.filter(status=AdvertiserInvitation.Status.PENDING, email__iexact=email,
                                        expires_at__lte=timezone.now()).update(
            status=AdvertiserInvitation.Status.REVOKED, responded_at=timezone.now())
        if organization.invitations.filter(status=AdvertiserInvitation.Status.PENDING, email__iexact=email).exists():
            raise ValidationError({"email": "An invitation is already waiting for this address."})
        invitation.save()
        # The invitee's address is not written to the audit trail.
        record_business_event("advertiser.member.invited", actor=user, target=organization, metadata={"role": role})
    return invitation


def pending_invitations_for(user):
    if advertiser_account_reason(user) != "ok":
        return AdvertiserInvitation.objects.none()
    return (AdvertiserInvitation.objects.select_related("organization")
            .filter(email__iexact=user.email, status=AdvertiserInvitation.Status.PENDING,
                    expires_at__gt=timezone.now()))


def _locked_invitation_for(user, invitation_id):
    invitation = (AdvertiserInvitation.objects.select_for_update().filter(pk=invitation_id, email__iexact=user.email)
                  .first())
    if invitation is None:
        raise NotFound("Invitation not found.")
    if invitation.status != AdvertiserInvitation.Status.PENDING or invitation.expires_at <= timezone.now():
        raise InvalidState("This invitation is no longer valid.")
    return invitation


def accept_invitation(user, invitation_id):
    reason = advertiser_account_reason(user)
    if reason != "ok":
        _deny("invitation.accept", user, None, reason)
    with transaction.atomic():
        invitation = _locked_invitation_for(user, invitation_id)
        organization = Organization.objects.select_for_update().get(pk=invitation.organization_id)
        if organization.verification_status in (V.SUSPENDED, V.REJECTED):
            raise InvalidState("This advertiser cannot accept new team members at the moment.")
        if not organization.memberships.filter(user=user, is_active=True).exists():
            AdvertiserMembership.objects.create(organization=organization, user=user, role=invitation.role)
        invitation.status = AdvertiserInvitation.Status.ACCEPTED
        invitation.responded_at = timezone.now()
        invitation.save(update_fields=["status", "responded_at", "updated_at"])
        record_business_event("advertiser.member.joined", actor=user, target=organization,
                              metadata={"role": invitation.role})
    return organization


def decline_invitation(user, invitation_id):
    with transaction.atomic():
        invitation = _locked_invitation_for(user, invitation_id)
        invitation.status = AdvertiserInvitation.Status.DECLINED
        invitation.responded_at = timezone.now()
        invitation.save(update_fields=["status", "responded_at", "updated_at"])
        record_business_event("advertiser.invitation.declined", actor=user, target=invitation.organization)


def revoke_invitation(user, organization, invitation_id):
    _require_member(user, organization, "invitation.revoke", owner=True)
    with transaction.atomic():
        invitation = (AdvertiserInvitation.objects.select_for_update()
                      .filter(pk=invitation_id, organization=organization).first())
        if invitation is None:
            raise NotFound("Invitation not found.")
        if invitation.status == AdvertiserInvitation.Status.PENDING:
            invitation.status = AdvertiserInvitation.Status.REVOKED
            invitation.responded_at = timezone.now()
            invitation.save(update_fields=["status", "responded_at", "updated_at"])
            record_business_event("advertiser.invitation.revoked", actor=user, target=organization)


def remove_member(user, organization, membership_id):
    """Owners remove anyone; a manager may remove only themselves. The last owner stays."""
    acting = _require_member(user, organization, "member.remove")
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=organization.pk)  # Serialize team changes.
        membership = organization.memberships.select_for_update().filter(pk=membership_id, is_active=True).first()
        if membership is None:
            raise NotFound("Team member not found.")
        if acting.role != ROLE.OWNER and membership.user_id != user.pk:
            _deny("member.remove", user, organization, "owner_required")
        if membership.role == ROLE.OWNER and not organization.memberships.filter(
                is_active=True, role=ROLE.OWNER).exclude(pk=membership.pk).exists():
            raise InvalidState("An advertiser must keep at least one owner.", code="last_owner")
        membership.is_active = False
        membership.removed_at = timezone.now()
        membership.save(update_fields=["is_active", "removed_at", "updated_at"])
        record_business_event("advertiser.member.removed", actor=user, target=organization,
                              metadata={"role": membership.role})


# --- Video grants --------------------------------------------------------------------------


def grant_video(actor, organization, video):
    _require_staff(can_manage_advertiser_videos, actor, "video.grant", organization)
    with transaction.atomic():
        organization = Organization.objects.select_for_update().get(pk=organization.pk)
        video = Video.objects.get(pk=video.pk)
        if video.publication_status == Video.Publication.ARCHIVED:
            raise InvalidState("Archived videos cannot be used in campaigns.")
        if organization.verification_status == V.REJECTED:
            raise InvalidState("Rejected advertisers cannot receive videos.")
        grant = organization.video_grants.filter(video=video, revoked_at__isnull=True).first()
        if grant is None:
            grant = AdvertiserVideo.objects.create(organization=organization, video=video, granted_by=actor)
            record_business_event("advertiser.video.granted", actor=actor, target=organization,
                                  metadata={"video": str(video.pk)})
    return grant


def revoke_video_grant(actor, organization, grant_id):
    _require_staff(can_manage_advertiser_videos, actor, "video.revoke", organization)
    with transaction.atomic():
        grant = (AdvertiserVideo.objects.select_for_update()
                 .filter(pk=grant_id, organization=organization, revoked_at__isnull=True).first())
        if grant is None:
            raise NotFound("Video authorization not found.")
        grant.revoked_at = timezone.now()
        grant.revoked_by = actor
        grant.save(update_fields=["revoked_at", "revoked_by", "updated_at"])
        record_business_event("advertiser.video.revoked", actor=actor, target=organization,
                              metadata={"video": str(grant.video_id)})


def granted_videos(organization):
    """Videos this organization may choose for campaigns (not archived)."""
    return (Video.objects.select_related("category")
            .filter(advertiser_grants__organization=organization, advertiser_grants__revoked_at__isnull=True)
            .exclude(publication_status=Video.Publication.ARCHIVED).distinct().order_by("title"))


def video_authorized(organization, video):
    return AdvertiserVideo.objects.filter(organization=organization, video=video, revoked_at__isnull=True).exists()


def video_available(video):
    """The video can be shown through the Phase 04 playback model right now."""
    return bool(video.is_playable and (not video.is_demo or demo_allowed())
                and (video.category_id is None or video.category.is_active))


# --- Campaigns: details -------------------------------------------------------------------


def _clean_campaign(organization, values, plans):
    now = timezone.now()
    errors = {}
    video = values.get("video")
    if video is None or not video_authorized(organization, video):
        errors["video"] = "Choose a video your advertiser account is authorized to use."
    elif video.publication_status == Video.Publication.ARCHIVED:
        errors["video"] = "This video is archived."
    starts, ends = values.get("starts_at"), values.get("ends_at")
    for name in ("starts_at", "ends_at"):
        value = values.get(name)
        if value is not None and timezone.is_naive(value):
            errors[name] = "Enter a date and time with a timezone."
    if starts and ends and not ({"starts_at", "ends_at"} & errors.keys()):
        if ends <= starts:
            errors["ends_at"] = "The end must be after the start."
        elif ends - starts > timedelta(days=MAX_CAMPAIGN_DAYS):
            errors["ends_at"] = f"Campaigns can run for at most {MAX_CAMPAIGN_DAYS} days."
        elif ends <= now:
            errors["ends_at"] = "The end must be in the future."
    for name in ("demo_budget_amount", "demo_reward_per_view"):
        if name in values:
            try:
                values[name] = to_amount(values[name])
            except (ValueError, TypeError):
                errors[name] = "Enter a whole number of shillings."
                continue
            if values[name] <= 0:
                errors[name] = "Enter an amount greater than zero."
    budget, rate = values.get("demo_budget_amount"), values.get("demo_reward_per_view")
    if isinstance(budget, int) and isinstance(rate, int) and rate > budget and "demo_reward_per_view" not in errors:
        errors["demo_reward_per_view"] = "The reward per view cannot exceed the budget."
    if plans is not None:
        plans = list(plans)
        if any(plan.status != MembershipPlan.Status.ACTIVE for plan in plans):
            errors["target_plans"] = "Only active membership plans can be targeted."
    if errors:
        raise ValidationError(errors)
    return values, plans


def create_campaign(user, organization, *, target_plans=(), **values):
    _require_member(user, organization, "campaign.create")
    if organization.verification_status in (V.SUSPENDED, V.REJECTED):
        raise InvalidState("Campaigns cannot be created while the advertiser is not in good standing.")
    values = {k: v for k, v in values.items() if k in CAMPAIGN_FIELDS}
    values, plans = _clean_campaign(organization, values, target_plans)
    with transaction.atomic():
        campaign = Campaign(organization=organization, created_by=user, **values)
        campaign.full_clean(exclude=["organization", "created_by"])
        campaign.save()
        campaign.target_plans.set(plans)
        record_business_event("campaign.created", actor=user, target=campaign, metadata={"revision": 1})
    return campaign


def _lock_campaign(campaign):
    return (Campaign.objects.select_for_update(of=("self",)).select_related("organization", "video",
                                                                            "video__category")
            .get(pk=campaign.pk))


def update_campaign(user, campaign, changes, *, expected_revision, target_plans=None):
    """Edit a DRAFT or REJECTED campaign. A rejected campaign returns to DRAFT."""
    _require_member(user, campaign.organization, "campaign.update")
    changes = {k: v for k, v in changes.items() if k in CAMPAIGN_FIELDS}
    with transaction.atomic():
        campaign = _lock_campaign(campaign)
        if campaign.revision != expected_revision:
            raise StaleDecision("This campaign changed since you opened it. Reload and try again.")
        if not campaign.is_editable:
            raise InvalidState("Only draft campaigns can be edited. Reopen it as a draft first.", code="not_editable")
        merged = {k: getattr(campaign, k) for k in CAMPAIGN_FIELDS}
        merged.update(changes)
        merged, plans = _clean_campaign(campaign.organization, merged, target_plans)
        changed = sorted(k for k in CAMPAIGN_FIELDS if merged[k] != getattr(campaign, k))
        if plans is not None and {p.pk for p in plans} != set(campaign.target_plans.values_list("pk", flat=True)):
            changed.append("target_plans")
        if not changed:
            return campaign
        for key in CAMPAIGN_FIELDS:
            setattr(campaign, key, merged[key])
        old_status = campaign.status
        campaign.revision += 1
        campaign.status = S.DRAFT
        campaign.full_clean(exclude=["organization", "created_by"])
        campaign.save()
        if plans is not None:
            campaign.target_plans.set(plans)
        record_business_event("campaign.updated", actor=user, target=campaign,
                              metadata={"revision": campaign.revision, "fields": changed})
        if old_status != S.DRAFT:
            record_business_event("campaign.reopened", actor=user, target=campaign,
                                  metadata={"from_status": old_status, "revision": campaign.revision, "reason": ""})
    return campaign


# --- Campaigns: lifecycle ------------------------------------------------------------------


def _event(name, actor, campaign, from_status, reason=""):
    record_business_event(f"campaign.{name}", actor=actor, target=campaign,
                          metadata={"from_status": from_status, "revision": campaign.revision,
                                    "reason": reason[:100]})


def _activation_blocker(campaign, now):
    """Why an approved campaign cannot run now ("" = it can)."""
    if not campaign.organization.is_approved:
        return "organization_not_approved"
    if not video_authorized(campaign.organization, campaign.video):
        return "video_not_authorized"
    if not video_available(campaign.video):
        return "video_unavailable"
    if campaign.ends_at <= now:
        return "ended"
    return ""


def submit_campaign(user, campaign):
    _require_member(user, campaign.organization, "campaign.submit")
    with transaction.atomic():
        campaign = _lock_campaign(campaign)
        if campaign.status != S.DRAFT:
            raise InvalidState("Only drafts can be submitted for review.")
        if not campaign.organization.is_approved:
            raise InvalidState("Your advertiser account must be approved before campaigns can be submitted.",
                               code="organization_not_approved")
        _clean_campaign(campaign.organization, {k: getattr(campaign, k) for k in CAMPAIGN_FIELDS}, None)
        if not video_available(campaign.video):
            raise InvalidState("The campaign video is not published and ready yet.", code="video_unavailable")
        campaign.status = S.SUBMITTED
        campaign.submitted_at = timezone.now()
        campaign.save()
        _event("submitted", user, campaign, S.DRAFT)
    return campaign


def withdraw_campaign(user, campaign):
    _require_member(user, campaign.organization, "campaign.withdraw")
    with transaction.atomic():
        campaign = _lock_campaign(campaign)
        if campaign.status != S.SUBMITTED:
            raise InvalidState("Only campaigns awaiting review can be withdrawn.")
        campaign.status = S.DRAFT
        campaign.save()
        _event("withdrawn", user, campaign, S.SUBMITTED)
    return campaign


def reopen_campaign(user, campaign):
    """Approved (scheduled or paused) campaign back to DRAFT: the approval no longer applies."""
    _require_member(user, campaign.organization, "campaign.reopen")
    with transaction.atomic():
        campaign = _lock_campaign(campaign)
        if campaign.status not in (S.SCHEDULED, S.PAUSED):
            raise InvalidState("Only scheduled or paused campaigns can be reopened for changes.")
        old = campaign.status
        campaign.status = S.DRAFT
        campaign.revision += 1
        campaign.paused_by_staff = False
        campaign.save()
        _event("reopened", user, campaign, old)
    return campaign


def _check_reviewer(actor, campaign, action):
    _require_staff(can_review_campaigns, actor, f"campaign.{action}", campaign)
    if campaign.organization.memberships.filter(user=actor, is_active=True).exists():
        _deny(f"campaign.{action}", actor, campaign, "self_review")


def approve_campaign(actor, campaign, *, expected_revision, notes=""):
    _check_reviewer(actor, campaign, "approve")  # Outside the transaction: denials are kept.
    with transaction.atomic():
        campaign = _lock_campaign(campaign)
        if campaign.status in APPROVED_STATES and campaign.approved_revision == expected_revision:
            return campaign  # Same decision submitted twice.
        if campaign.revision != expected_revision:
            raise StaleDecision("The advertiser changed this campaign. Review the new version.")
        if campaign.status != S.SUBMITTED:
            raise InvalidState("Only campaigns awaiting review can be approved.")
        now = timezone.now()
        blocker = _activation_blocker(campaign, now)
        if blocker:
            raise InvalidState("This campaign cannot be approved: " + blocker.replace("_", " ") + ".", code=blocker)
        campaign.status = S.SCHEDULED
        campaign.approved_revision = campaign.revision
        campaign.reviewed_at = now
        campaign.reviewed_by = actor
        campaign.review_notes = _reason(notes, required=False)
        campaign.save()
        _event("approved", actor, campaign, S.SUBMITTED)
        _activate_if_due(campaign, now)
    return campaign


def reject_campaign(actor, campaign, *, expected_revision, reason):
    reason = _reason(reason, required=True)
    _check_reviewer(actor, campaign, "reject")
    with transaction.atomic():
        campaign = _lock_campaign(campaign)
        if campaign.status == S.REJECTED and campaign.revision == expected_revision:
            return campaign
        if campaign.revision != expected_revision:
            raise StaleDecision("The advertiser changed this campaign. Review the new version.")
        if campaign.status != S.SUBMITTED:
            raise InvalidState("Only campaigns awaiting review can be rejected.")
        campaign.status = S.REJECTED
        campaign.reviewed_at = timezone.now()
        campaign.reviewed_by = actor
        campaign.review_notes = reason
        campaign.save()
        _event("rejected", actor, campaign, S.SUBMITTED, reason)
    return campaign


def _actor_side(actor, campaign, action):
    """("staff" | "advertiser") after checking the actor may manage this campaign."""
    if getattr(actor, "is_staff", False):
        _check_reviewer(actor, campaign, action)
        return "staff"
    _require_member(actor, campaign.organization, f"campaign.{action}")
    return "advertiser"


def pause_campaign(actor, campaign, *, reason=""):
    side = _actor_side(actor, campaign, "pause")
    reason = _reason(reason, required=side == "staff")
    with transaction.atomic():
        campaign = _lock_campaign(campaign)
        if campaign.status == S.PAUSED:
            return campaign
        if campaign.status not in (S.SCHEDULED, S.ACTIVE):
            raise InvalidState("Only scheduled or active campaigns can be paused.")
        old = campaign.status
        campaign.status = S.PAUSED
        campaign.paused_by_staff = side == "staff"
        campaign.save()
        _event("paused", actor, campaign, old, reason)
    return campaign


def resume_campaign(actor, campaign):
    side = _actor_side(actor, campaign, "resume")
    with transaction.atomic():
        campaign = _lock_campaign(campaign)
        if campaign.status != S.PAUSED:
            raise InvalidState("Only paused campaigns can be resumed.")
        if campaign.paused_by_staff and side != "staff":
            raise InvalidState("This campaign was paused by ClipCash staff. Please contact support.",
                               code="paused_by_staff")
        now = timezone.now()
        blocker = _activation_blocker(campaign, now)
        if blocker:
            raise InvalidState("This campaign cannot resume: " + blocker.replace("_", " ") + ".", code=blocker)
        campaign.paused_by_staff = False
        if campaign.starts_at <= now:
            campaign.status = S.ACTIVE
            campaign.activated_at = campaign.activated_at or now
        else:
            campaign.status = S.SCHEDULED
        campaign.save()
        _event("resumed", actor, campaign, S.PAUSED)
    return campaign


def cancel_campaign(actor, campaign, *, reason=""):
    side = _actor_side(actor, campaign, "cancel")
    reason = _reason(reason, required=side == "staff")
    with transaction.atomic():
        campaign = _lock_campaign(campaign)
        if campaign.status == S.CANCELLED:
            return campaign
        if campaign.status in (S.COMPLETED, S.ARCHIVED):
            raise InvalidState("Finished campaigns cannot be cancelled.")
        old = campaign.status
        campaign.status = S.CANCELLED
        campaign.cancelled_at = timezone.now()
        campaign.save()
        _event("cancelled", actor, campaign, old, reason)
    return campaign


def archive_campaign(actor, campaign):
    _actor_side(actor, campaign, "archive")
    with transaction.atomic():
        campaign = _lock_campaign(campaign)
        if campaign.status == S.ARCHIVED:
            return campaign
        if campaign.status not in (S.COMPLETED, S.CANCELLED, S.REJECTED):
            raise InvalidState("Only completed, cancelled or rejected campaigns can be archived.")
        old = campaign.status
        campaign.status = S.ARCHIVED
        campaign.archived_at = timezone.now()
        campaign.save()
        _event("archived", actor, campaign, old)
    return campaign


def _activate_if_due(campaign, now):
    """Inside a transaction holding the campaign lock."""
    if campaign.status == S.SCHEDULED and campaign.starts_at <= now and not _activation_blocker(campaign, now):
        campaign.status = S.ACTIVE
        campaign.activated_at = now
        campaign.save()
        _event("activated", None, campaign, S.SCHEDULED)
        return True
    return False


def run_schedule(now=None, limit=200):
    """Background job: start due campaigns and complete ended ones. Idempotent and
    safe to run concurrently (rows are claimed with SKIP LOCKED)."""
    now = now or timezone.now()
    counts = {"activated": 0, "completed": 0}
    with transaction.atomic():
        ended = (Campaign.objects.select_for_update(skip_locked=True, of=("self",))
                 .filter(status__in=[S.SCHEDULED, S.ACTIVE, S.PAUSED], ends_at__lte=now).order_by("ends_at")[:limit])
        for campaign in ended:
            old = campaign.status
            campaign.status = S.COMPLETED
            campaign.completed_at = now
            campaign.save()
            _event("completed", None, campaign, old)
            counts["completed"] += 1
        due = (Campaign.objects.select_for_update(skip_locked=True, of=("self",))
               .select_related("organization", "video", "video__category")
               .filter(status=S.SCHEDULED, starts_at__lte=now, ends_at__gt=now).order_by("starts_at")[:limit])
        for campaign in due:
            counts["activated"] += _activate_if_due(campaign, now)
    return counts


# --- Funding firewall -------------------------------------------------------------------------


def request_funding_status(actor, campaign, requested_status):
    """Always refused before Phase 08: an entered budget or a staff declaration is
    never proof of advertiser money. Records a security event."""
    record_security_event("campaign.funding.refused", outcome=DENIED,
                          actor=actor if getattr(actor, "is_authenticated", False) else None, target=campaign,
                          metadata={"requested_status": str(requested_status)[:30],
                                    "reason": "reconciled_funding_required"})
    raise FundingLocked("Campaign funding can only be verified from reconciled advertiser payments, "
                        "which are not available yet.")


# --- Eligibility (Phase 06 boundary) ------------------------------------------------------------


@dataclass(frozen=True)
class EligibilityDecision:
    eligible: bool
    reason: str
    # Demo visibility vs money: no campaign is financially eligible before Phase 08.
    demo_only: bool = True
    financially_eligible: bool = False

    def __bool__(self):
        return self.eligible


ELIGIBILITY_MESSAGES = {
    "ok": "Eligible members can be shown this campaign.",
    "feature_disabled": "Advertiser features are switched off.",
    "unsupported_campaign": "This campaign type is not supported.",
    "campaign_not_active": "The campaign is not active.",
    "not_started": "The campaign has not started yet.",
    "ended": "The campaign has ended.",
    "organization_not_approved": "The advertiser is not approved.",
    "video_not_authorized": "The video is no longer authorized for this advertiser.",
    "video_unavailable": "The video is not published and ready.",
    "plan_not_targeted": "The member's plan is not targeted.",
}


def eligibility_message(reason):
    return ELIGIBILITY_MESSAGES.get(reason, "The member is not eligible (" + reason.replace("_", " ") + ").")


def campaign_eligibility(campaign, member=None, now=None):
    """
    Server-side campaign eligibility (default deny). Reasons:
    feature_disabled, unsupported_campaign, campaign_not_active, not_started,
    ended, organization_not_approved, video_not_authorized, video_unavailable,
    <membership access reasons>, plan_not_targeted, ok.
    ``member=None`` checks the campaign alone.
    """
    now = now or timezone.now()
    if not features.is_enabled("advertisers"):
        return EligibilityDecision(False, "feature_disabled")
    if campaign.kind != Campaign.Kind.VIDEO_VIEW:
        return EligibilityDecision(False, "unsupported_campaign")
    if campaign.status != S.ACTIVE:
        return EligibilityDecision(False, "campaign_not_active")
    if campaign.starts_at > now:
        return EligibilityDecision(False, "not_started")
    if campaign.ends_at <= now:
        return EligibilityDecision(False, "ended")
    blocker = _activation_blocker(campaign, now)
    if blocker:
        return EligibilityDecision(False, blocker)
    if member is None:
        return EligibilityDecision(True, "ok", financially_eligible=campaign.financially_eligible)
    access = check_access(member, entitlement=campaign.video.required_entitlement, now=now)
    if not access:
        return EligibilityDecision(False, access.reason)
    plan_ids = [plan.pk for plan in campaign.target_plans.all()]
    if plan_ids and access.subscription.plan_id not in plan_ids:
        return EligibilityDecision(False, "plan_not_targeted")
    return EligibilityDecision(True, "ok", financially_eligible=campaign.financially_eligible)


def eligible_campaigns(member, now=None):
    """Active campaigns ``member`` is eligible for right now (for Phase 06)."""
    now = now or timezone.now()
    candidates = (Campaign.objects.select_related("organization", "video", "video__category")
                  .prefetch_related("target_plans")
                  .filter(status=S.ACTIVE, starts_at__lte=now, ends_at__gt=now,
                          organization__verification_status=V.APPROVED))
    return [c for c in candidates if campaign_eligibility(c, member, now)]


# --- Preview -------------------------------------------------------------------------------------


def preview_playback(user, campaign):
    """Short-lived signed preview for the advertiser's own authorized video, or a
    reviewer. Same provider signing as member playback; no credentials exposed."""
    if not (can_review_campaigns(user) or membership_for(user, campaign.organization)):
        raise NotFound("Campaign not found.")
    video = campaign.video
    if not video_authorized(campaign.organization, video) and not can_review_campaigns(user):
        return "video_not_authorized", None
    if not video_available(video):
        return "video_unavailable", None
    try:
        return "ok", get_provider(video.provider).playback(video.provider_video_id,
                                                           expires_in=settings.VIDEO_PLAYBACK_TOKEN_SECONDS)
    except ProviderError:
        return "video_unavailable", None


# --- Reporting ---------------------------------------------------------------------------------


def campaign_summary(queryset):
    """Counts by status and demo budget totals in one query. Demo figures are not money."""
    counts = {status: Count("id", filter=Q(status=status)) for status in S.values}
    totals = queryset.aggregate(
        total=Count("id"),
        demo_budget_running=Sum("demo_budget_amount", filter=Q(status__in=list(APPROVED_STATES)), default=0),
        demo_budget_all=Sum("demo_budget_amount", filter=~Q(status__in=[S.CANCELLED, S.ARCHIVED]), default=0),
        **{f"n_{status.lower()}": expr for status, expr in counts.items()},
    )
    return totals


def organization_report(organization):
    return campaign_summary(Campaign.objects.filter(organization=organization))
