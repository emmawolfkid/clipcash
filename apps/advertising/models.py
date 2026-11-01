"""
Advertisers and campaigns (Phase 05). Use apps.advertising.services.

* Advertisers are organizations. Ordinary verified member accounts join them
  through AdvertiserMembership (OWNER / MANAGER); platform roles and account
  status are never changed by organization membership. Staff accounts cannot
  belong to an organization (no self-approval).
* An organization uses a Phase 04 video only through an explicit staff grant
  (AdvertiserVideo); media is never duplicated.
* Campaign budgets are DEMONSTRATION figures only. They are not money: nothing
  here is written to the ledger, and funding_status is locked to UNFUNDED by
  the database until Phase 08 adds reconciled advertiser funding.
* Approval covers one exact revision: any change returns the campaign to
  DRAFT with a new revision, so it must be reviewed again.

PostgreSQL guards (migration 0002): no deletes, valid status transitions,
ARCHIVED terminal.
"""

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from apps.core.fields import AmountField
from apps.core.models import BaseModel
from apps.core.money import DEFAULT_CURRENCY


class Organization(BaseModel):
    class Verification(models.TextChoices):
        PENDING = "PENDING", _("Awaiting review")
        APPROVED = "APPROVED", _("Approved")
        REJECTED = "REJECTED", _("Rejected")
        SUSPENDED = "SUSPENDED", _("Suspended")

    display_name = models.CharField(_("display name"), max_length=100)
    legal_name = models.CharField(_("legal name"), max_length=200)
    contact_email = models.EmailField(_("contact email"))
    contact_phone = models.CharField(_("contact phone"), max_length=20, blank=True)
    website = models.URLField(_("website"), max_length=200, blank=True)
    country = models.CharField(_("country"), max_length=2, default="TZ",
                               help_text=_("ISO 3166-1 alpha-2 code, e.g. TZ."))
    # Text references only (no document uploads); visible to the organization
    # and to staff who review advertisers.
    registration_number = models.CharField(_("business registration number"), max_length=50, blank=True)
    tax_id = models.CharField(_("tax identification number (TIN)"), max_length=50, blank=True)
    verification_status = models.CharField(_("verification"), max_length=10, choices=Verification.choices,
                                           default=Verification.PENDING)
    submitted_at = models.DateTimeField(_("submitted for review"), null=True, blank=True)
    reviewed_at = models.DateTimeField(_("reviewed"), null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=_("reviewed by"))
    review_reason = models.CharField(_("review reason"), max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+", verbose_name=_("created by"))

    class Meta:
        verbose_name = _("advertiser")
        verbose_name_plural = _("advertisers")
        ordering = ["display_name"]
        permissions = [
            ("review_advertisers", "Can review and verify advertisers"),
            ("review_campaigns", "Can review and moderate advertising campaigns"),
            ("manage_advertiser_videos", "Can authorize advertisers to use videos"),
        ]
        indexes = [models.Index(fields=["verification_status", "submitted_at"], name="adv_org_review_idx")]
        constraints = [
            models.UniqueConstraint(Lower("display_name"), name="adv_org_name_ci_unique",
                                    violation_error_message=_("An advertiser with this name already exists.")),
            models.CheckConstraint(condition=~Q(display_name="") & ~Q(legal_name=""), name="adv_org_names_not_blank"),
            models.CheckConstraint(condition=Q(country__regex=r"^[A-Z]{2}$"), name="adv_org_country_code"),
            models.CheckConstraint(condition=Q(verification_status__in=["PENDING", "APPROVED", "REJECTED",
                                                                        "SUSPENDED"]),
                                   name="adv_org_verification_valid"),
            models.CheckConstraint(
                condition=Q(verification_status="PENDING") | (Q(reviewed_at__isnull=False)
                                                              & Q(reviewed_by__isnull=False)),
                name="adv_org_decision_recorded"),
        ]

    def __str__(self):
        return self.display_name

    @property
    def is_approved(self):
        return self.verification_status == self.Verification.APPROVED


class AdvertiserMembership(BaseModel):
    class Role(models.TextChoices):
        OWNER = "OWNER", _("Owner")
        MANAGER = "MANAGER", _("Campaign manager")

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="advertiser_memberships")
    role = models.CharField(_("role"), max_length=10, choices=Role.choices, default=Role.MANAGER)
    is_active = models.BooleanField(_("active"), default=True)
    removed_at = models.DateTimeField(_("removed"), null=True, blank=True)

    class Meta:
        verbose_name = _("advertiser team member")
        verbose_name_plural = _("advertiser team members")
        constraints = [
            models.UniqueConstraint(fields=["organization", "user"], condition=Q(is_active=True),
                                    name="adv_member_active_unique"),
            models.CheckConstraint(condition=Q(role__in=["OWNER", "MANAGER"]), name="adv_member_role_valid"),
            models.CheckConstraint(condition=Q(is_active=True, removed_at__isnull=True)
                                   | Q(is_active=False, removed_at__isnull=False),
                                   name="adv_member_removed_consistent"),
        ]

    def __str__(self):
        return f"{self.user} @ {self.organization}"


class AdvertiserInvitation(BaseModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        ACCEPTED = "ACCEPTED", _("Accepted")
        DECLINED = "DECLINED", _("Declined")
        REVOKED = "REVOKED", _("Revoked")

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="invitations")
    email = models.EmailField(_("email"))
    role = models.CharField(_("role"), max_length=10, choices=AdvertiserMembership.Role.choices,
                            default=AdvertiserMembership.Role.MANAGER)
    status = models.CharField(_("status"), max_length=10, choices=Status.choices, default=Status.PENDING)
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    expires_at = models.DateTimeField(_("expires"))
    responded_at = models.DateTimeField(_("responded"), null=True, blank=True)

    class Meta:
        verbose_name = _("advertiser invitation")
        constraints = [
            models.UniqueConstraint(F("organization"), Lower("email"), condition=Q(status="PENDING"),
                                    name="adv_invite_pending_unique"),
            models.CheckConstraint(condition=Q(role__in=["OWNER", "MANAGER"]), name="adv_invite_role_valid"),
        ]

    def __str__(self):
        return f"{self.email} → {self.organization}"


class AdvertiserVideo(BaseModel):
    """Staff grant: this organization may use this Phase 04 video in campaigns."""

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="video_grants")
    video = models.ForeignKey("videos.Video", on_delete=models.PROTECT, related_name="advertiser_grants")
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    revoked_at = models.DateTimeField(_("revoked"), null=True, blank=True)
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")

    class Meta:
        verbose_name = _("advertiser video")
        constraints = [
            models.UniqueConstraint(fields=["organization", "video"], condition=Q(revoked_at__isnull=True),
                                    name="adv_video_grant_unique"),
        ]

    def __str__(self):
        return f"{self.video} → {self.organization}"


class Campaign(BaseModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", _("Draft")
        SUBMITTED = "SUBMITTED", _("Awaiting review")
        REJECTED = "REJECTED", _("Changes requested")
        SCHEDULED = "SCHEDULED", _("Approved, scheduled")
        ACTIVE = "ACTIVE", _("Active")
        PAUSED = "PAUSED", _("Paused")
        COMPLETED = "COMPLETED", _("Completed")
        CANCELLED = "CANCELLED", _("Cancelled")
        ARCHIVED = "ARCHIVED", _("Archived")

    class Kind(models.TextChoices):
        VIDEO_VIEW = "VIDEO_VIEW", _("Sponsored video view")

    class Funding(models.TextChoices):
        # Only UNFUNDED is accepted by the database until Phase 08 adds
        # reconciled advertiser funding (see migration 0002).
        UNFUNDED = "UNFUNDED", _("Not funded (demonstration only)")
        PENDING_RECONCILIATION = "PENDING_RECONCILIATION", _("Awaiting reconciliation")
        VERIFIED = "VERIFIED", _("Funding verified")
        REJECTED = "REJECTED", _("Funding rejected")

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="campaigns")
    name = models.CharField(_("name"), max_length=120)
    description = models.TextField(_("description"), max_length=2000, blank=True)
    kind = models.CharField(_("campaign type"), max_length=12, choices=Kind.choices, default=Kind.VIDEO_VIEW)
    video = models.ForeignKey("videos.Video", on_delete=models.PROTECT, related_name="campaigns")
    starts_at = models.DateTimeField(_("starts"))
    ends_at = models.DateTimeField(_("ends"))
    # Targeting: empty = any member with an active membership.
    target_plans = models.ManyToManyField("memberships.MembershipPlan", blank=True, related_name="+",
                                          verbose_name=_("membership plans"))
    max_views_per_member = models.PositiveSmallIntegerField(_("views per member"), default=1)
    # Demonstration figures (whole shillings). Not money, never in the ledger.
    demo_budget_amount = AmountField(_("demo budget (TZS)"), non_negative=True)
    demo_reward_per_view = AmountField(_("demo reward per view (TZS)"), non_negative=True)
    demo_currency = models.CharField(_("currency"), max_length=3, default=DEFAULT_CURRENCY)
    funding_status = models.CharField(_("funding"), max_length=24, choices=Funding.choices,
                                      default=Funding.UNFUNDED)

    status = models.CharField(_("status"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    revision = models.PositiveIntegerField(_("revision"), default=1)
    approved_revision = models.PositiveIntegerField(_("approved revision"), null=True, blank=True)
    submitted_at = models.DateTimeField(_("submitted"), null=True, blank=True)
    reviewed_at = models.DateTimeField(_("reviewed"), null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    review_notes = models.CharField(_("review notes"), max_length=500, blank=True)
    activated_at = models.DateTimeField(_("activated"), null=True, blank=True)
    paused_by_staff = models.BooleanField(_("paused by staff"), default=False)
    completed_at = models.DateTimeField(_("completed"), null=True, blank=True)
    cancelled_at = models.DateTimeField(_("cancelled"), null=True, blank=True)
    archived_at = models.DateTimeField(_("archived"), null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")

    class Meta:
        verbose_name = _("campaign")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "status"], name="adv_campaign_org_idx"),
            models.Index(fields=["status", "submitted_at"], name="adv_campaign_review_idx"),
            models.Index(fields=["status", "starts_at", "ends_at"], name="adv_campaign_schedule_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=~Q(name=""), name="adv_campaign_name_not_blank"),
            models.CheckConstraint(condition=Q(ends_at__gt=F("starts_at")), name="adv_campaign_dates_ordered"),
            models.CheckConstraint(condition=Q(max_views_per_member__gte=1, max_views_per_member__lte=10),
                                   name="adv_campaign_frequency_range"),
            models.CheckConstraint(condition=Q(demo_budget_amount__gt=0) & Q(demo_reward_per_view__gt=0)
                                   & Q(demo_reward_per_view__lte=F("demo_budget_amount")),
                                   name="adv_campaign_demo_budget_valid"),
            models.CheckConstraint(condition=Q(demo_currency="TZS"), name="adv_campaign_currency_supported"),
            # Financial firewall: no campaign can be funded until Phase 08.
            models.CheckConstraint(condition=Q(funding_status="UNFUNDED"), name="adv_campaign_funding_locked"),
            models.CheckConstraint(condition=Q(status__in=["DRAFT", "SUBMITTED", "REJECTED", "SCHEDULED", "ACTIVE",
                                                           "PAUSED", "COMPLETED", "CANCELLED", "ARCHIVED"]),
                                   name="adv_campaign_status_valid"),
            # Approved states always run exactly the reviewed revision.
            models.CheckConstraint(
                condition=~Q(status__in=["SCHEDULED", "ACTIVE", "PAUSED", "COMPLETED"])
                | (Q(approved_revision=F("revision")) & Q(reviewed_at__isnull=False) & Q(reviewed_by__isnull=False)),
                name="adv_campaign_approved_revision"),
            models.CheckConstraint(condition=~Q(status="ACTIVE") | Q(activated_at__isnull=False),
                                   name="adv_campaign_active_has_time"),
            models.CheckConstraint(condition=~Q(status="ARCHIVED") | Q(archived_at__isnull=False),
                                   name="adv_campaign_archived_has_time"),
        ]

    def __str__(self):
        return self.name

    @property
    def is_editable(self):
        return self.status in (self.Status.DRAFT, self.Status.REJECTED)

    @property
    def financially_eligible(self):
        """Real-money eligibility. Always False until Phase 08 (locked in code and database)."""
        from apps.core import features

        return self.funding_status == self.Funding.VERIFIED and features.is_enabled("real_money_rewards")

    @property
    def demo_view_capacity(self):
        return self.demo_budget_amount // self.demo_reward_per_view if self.demo_reward_per_view else 0
