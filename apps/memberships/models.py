"""
Membership plans, immutable plan versions and member subscriptions.

PostgreSQL enforces (migration 0003): subscription snapshots are immutable,
subscriptions are created PENDING and can only follow the documented state
machine, plan versions are append-only, and (exclusion constraint) a member
can never have overlapping active or suspended membership periods.
Use apps.memberships.services for every change.
"""

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Func, Q
from django.utils.translation import gettext_lazy as _

from apps.core.fields import AmountField
from apps.core.models import BaseModel
from apps.core.money import DEFAULT_CURRENCY, MAX_AMOUNT, SUPPORTED_CURRENCIES

from .entitlements import ENTITLEMENTS

MAX_DURATION_DAYS = 3660
MAX_BENEFITS = 10
MAX_BENEFIT_LENGTH = 120


class TsTzRange(Func):
    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


def validate_benefits(value):
    if not isinstance(value, list) or len(value) > MAX_BENEFITS:
        raise ValidationError(_("List at most %(n)s benefits.") % {"n": MAX_BENEFITS})
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > MAX_BENEFIT_LENGTH:
            raise ValidationError(
                _("Each benefit must be text of at most %(n)s characters.") % {"n": MAX_BENEFIT_LENGTH}
            )


def validate_entitlements(value):
    if not isinstance(value, list) or len(set(value)) != len(value):
        raise ValidationError(_("Entitlements must be a list without duplicates."))
    unknown = [code for code in value if code not in ENTITLEMENTS]
    if unknown:
        raise ValidationError(_("Unknown entitlements: %(codes)s.") % {"codes": ", ".join(map(str, unknown))})


class MembershipPlan(BaseModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", _("Active")
        INACTIVE = "INACTIVE", _("Inactive")
        ARCHIVED = "ARCHIVED", _("Archived")

    code = models.SlugField(_("code"), max_length=50, unique=True,
                            help_text=_("Stable identifier; cannot be changed after creation."))
    name = models.CharField(_("name"), max_length=100)
    description = models.TextField(_("description"), max_length=1000, blank=True)
    price_amount = AmountField(_("price (TZS)"), non_negative=True)
    currency = models.CharField(_("currency"), max_length=3, default=DEFAULT_CURRENCY)
    duration_days = models.PositiveSmallIntegerField(_("duration (days)"))
    benefits = models.JSONField(_("benefits"), default=list, blank=True, validators=[validate_benefits])
    entitlements = models.JSONField(_("entitlements"), default=list, blank=True,
                                    validators=[validate_entitlements])
    display_order = models.PositiveSmallIntegerField(_("display order"), default=100)
    status = models.CharField(_("status"), max_length=10, choices=Status.choices, default=Status.ACTIVE)
    is_public = models.BooleanField(_("publicly listed"), default=True)
    version = models.PositiveIntegerField(_("version"), default=1, editable=False)

    class Meta:
        verbose_name = _("membership plan")
        verbose_name_plural = _("membership plans")
        ordering = ["display_order", "price_amount", "name"]
        constraints = [
            models.CheckConstraint(condition=Q(price_amount__gt=0, price_amount__lte=MAX_AMOUNT),
                                   name="plan_price_positive"),
            models.CheckConstraint(condition=Q(duration_days__gte=1, duration_days__lte=MAX_DURATION_DAYS),
                                   name="plan_duration_range"),
            models.CheckConstraint(condition=Q(currency__in=SUPPORTED_CURRENCIES), name="plan_currency"),
            models.CheckConstraint(condition=Q(status__in=["ACTIVE", "INACTIVE", "ARCHIVED"]), name="plan_status"),
            models.CheckConstraint(condition=~Q(name=""), name="plan_name_not_blank"),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"

    @property
    def purchasable(self):
        return self.status == self.Status.ACTIVE and self.is_public


class MembershipPlanVersion(BaseModel):
    """Append-only record of a plan's commercial terms at each version."""

    plan = models.ForeignKey(MembershipPlan, on_delete=models.PROTECT, related_name="versions")
    version = models.PositiveIntegerField(_("version"))
    name = models.CharField(_("name"), max_length=100)
    price_amount = AmountField(_("price (TZS)"), non_negative=True)
    currency = models.CharField(_("currency"), max_length=3, default=DEFAULT_CURRENCY)
    duration_days = models.PositiveSmallIntegerField(_("duration (days)"))
    benefits = models.JSONField(_("benefits"), default=list)
    entitlements = models.JSONField(_("entitlements"), default=list)
    created_by_id = models.UUIDField(_("created by"), null=True, blank=True)

    class Meta:
        verbose_name = _("membership plan version")
        verbose_name_plural = _("membership plan versions")
        ordering = ["plan", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "version"], name="plan_version_unique"),
            models.CheckConstraint(condition=Q(price_amount__gt=0), name="plan_version_price_positive"),
        ]

    def __str__(self):
        return f"{self.plan.code} v{self.version}"


class Subscription(BaseModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending payment")
        ACTIVE = "ACTIVE", _("Active")
        EXPIRED = "EXPIRED", _("Expired")
        CANCELLED = "CANCELLED", _("Cancelled")
        SUSPENDED = "SUSPENDED", _("Suspended")

    class Source(models.TextChoices):
        PAYMENT = "PAYMENT", _("Verified payment")
        COMPLIMENTARY = "COMPLIMENTARY", _("Complimentary grant")

    class Reason(models.TextChoices):
        MEMBER_REQUEST = "MEMBER_REQUEST", _("Member request")
        REPLACED = "REPLACED", _("Replaced by another choice")
        UNPAID_TIMEOUT = "UNPAID_TIMEOUT", _("Not paid in time")
        ADMIN_REVIEW = "ADMIN_REVIEW", _("Administrative review")
        POLICY_VIOLATION = "POLICY_VIOLATION", _("Policy violation")
        FRAUD_SUSPECTED = "FRAUD_SUSPECTED", _("Suspected fraud")
        REFUND = "REFUND", _("Refunded")
        ACCOUNT_CLOSED = "ACCOUNT_CLOSED", _("Account closed")

    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="subscriptions")
    plan = models.ForeignKey(MembershipPlan, on_delete=models.PROTECT, related_name="subscriptions")
    plan_version = models.ForeignKey(MembershipPlanVersion, on_delete=models.PROTECT, related_name="subscriptions")

    # Snapshots of the terms the member chose (immutable; database-enforced).
    plan_name = models.CharField(_("plan name"), max_length=100)
    price_amount = AmountField(_("price (TZS)"), non_negative=True)
    currency = models.CharField(_("currency"), max_length=3, default=DEFAULT_CURRENCY)
    duration_days = models.PositiveSmallIntegerField(_("duration (days)"))
    benefits = models.JSONField(_("benefits"), default=list)
    entitlements = models.JSONField(_("entitlements"), default=list)

    status = models.CharField(_("status"), max_length=10, choices=Status.choices, default=Status.PENDING)
    pending_until = models.DateTimeField(_("pending until"))
    starts_at = models.DateTimeField(_("starts"), null=True, blank=True)
    expires_at = models.DateTimeField(_("expires"), null=True, blank=True)

    activation_source = models.CharField(_("activation source"), max_length=15, choices=Source.choices, blank=True)
    payment_reference = models.CharField(_("verified payment reference"), max_length=100, blank=True)
    amount_paid = AmountField(_("amount paid (TZS)"), non_negative=True, null=True, blank=True)
    renewal_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="renewals")

    activated_at = models.DateTimeField(_("activated at"), null=True, blank=True)
    activated_by_id = models.UUIDField(_("activated by"), null=True, blank=True)
    cancelled_at = models.DateTimeField(_("cancelled at"), null=True, blank=True)
    cancellation_reason = models.CharField(_("cancellation reason"), max_length=20, choices=Reason.choices,
                                           blank=True)
    suspended_at = models.DateTimeField(_("suspended at"), null=True, blank=True)
    suspension_reason = models.CharField(_("suspension reason"), max_length=20, choices=Reason.choices,
                                         blank=True)
    expired_at = models.DateTimeField(_("expired at"), null=True, blank=True)
    note = models.CharField(_("administrator note"), max_length=200, blank=True)

    class Meta:
        verbose_name = _("subscription")
        verbose_name_plural = _("subscriptions")
        ordering = ["-created_at"]
        permissions = [
            ("view_memberships", "Can view member subscriptions"),
            ("manage_memberships", "Can suspend, reinstate or cancel subscriptions"),
        ]
        indexes = [
            models.Index(fields=["member", "-created_at"], name="sub_member_idx"),
            models.Index(fields=["status", "expires_at"], name="sub_status_expiry_idx"),
            models.Index(fields=["pending_until"], name="sub_pending_idx", condition=Q(status="PENDING")),
        ]
        constraints = [
            # A member never has overlapping periods that grant (or could grant) access.
            ExclusionConstraint(
                name="sub_no_overlapping_periods",
                expressions=[
                    (TsTzRange("starts_at", "expires_at"), RangeOperators.OVERLAPS),
                    ("member", RangeOperators.EQUAL),
                ],
                condition=Q(status__in=["ACTIVE", "SUSPENDED"]),
            ),
            # At most one unpaid choice per member at a time.
            models.UniqueConstraint(fields=["member"], condition=Q(status="PENDING"), name="sub_one_pending"),
            models.UniqueConstraint(fields=["payment_reference"], condition=~Q(payment_reference=""),
                                    name="sub_payment_reference_unique"),
            models.CheckConstraint(
                condition=Q(status__in=["PENDING", "ACTIVE", "EXPIRED", "CANCELLED", "SUSPENDED"]),
                name="sub_status_valid"),
            models.CheckConstraint(condition=Q(currency__in=SUPPORTED_CURRENCIES), name="sub_currency"),
            models.CheckConstraint(condition=Q(price_amount__gt=0), name="sub_price_positive"),
            # Activated subscriptions have a period and a valid activation record.
            models.CheckConstraint(
                condition=Q(activated_at__isnull=True)
                | Q(starts_at__isnull=False, expires_at__isnull=False, expires_at__gt=F("starts_at")),
                name="sub_activated_has_period"),
            models.CheckConstraint(
                condition=~Q(status__in=["ACTIVE", "SUSPENDED", "EXPIRED"]) | Q(activated_at__isnull=False),
                name="sub_live_states_activated"),
            models.CheckConstraint(
                condition=Q(activated_at__isnull=True, activation_source="", payment_reference="",
                            amount_paid__isnull=True)
                | Q(activation_source="PAYMENT", amount_paid=F("price_amount")) & ~Q(payment_reference="")
                | Q(activation_source="COMPLIMENTARY", amount_paid=0, payment_reference=""),
                name="sub_activation_source_consistent"),
        ]

    def __str__(self):
        return f"{self.member} - {self.plan_name} [{self.status}]"

    def is_in_force(self, now):
        return (self.status == self.Status.ACTIVE and self.starts_at is not None
                and self.starts_at <= now < self.expires_at)
