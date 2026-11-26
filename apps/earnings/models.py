"""
FIN-01: real TZS rewards and campaign funding. Use apps.earnings.services and apps.earnings.funding.

Money only moves through apps.wallets.services (the double-entry ledger). These tables record
the business decisions around those postings; no balance is stored here, and wallet balances are
always the ledger projection (apps.wallets.wallet_services.balances).

* CampaignDeposit: a manual deposit (Airtel Lipa or bank transfer) by an advertiser organization,
  including ClipCash's own organization. Recording it moves no money: only a different staff
  member's verification of the actual receipt posts it to the ledger (four-eyes, database-enforced).
* FundingMovement: verified funds committed to, or released from, one campaign's restricted
  reward funding (one ledger entry each).
* SessionReward: exactly one real reward per watch session. Created RESERVED when a session
  starts with a TZS offer (holding funding and daily-limit capacity), then PENDING (posted to the
  member's pending balance), AVAILABLE (cleared), or RELEASED / REVERSED / RECOVERY_REQUIRED.
* EarningSettings / PlanEarningLimit: staff-configured daily limits and the clearance period.

Simulated credits (apps.rewards.DemoReward) are never read or converted here.
Database guards: migration 0002.
"""

import secrets

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _

from apps.core.fields import AmountField
from apps.core.models import BaseModel

MAX_CLEARANCE_HOURS = 720  # 30 days


def new_deposit_reference():
    return "DEP-" + secrets.token_hex(4).upper()


class EarningSettings(models.Model):
    """Platform-wide earning rules (a single row, id = 1). Empty limits mean "no platform limit"."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    daily_amount_limit = AmountField(_("daily earning limit per member (TZS)"), non_negative=True, null=True,
                                     blank=True)
    daily_view_limit = models.PositiveSmallIntegerField(_("daily rewarded views per member"), null=True, blank=True)
    clearance_hours = models.PositiveSmallIntegerField(_("clearance period (hours)"), default=24)
    version = models.PositiveIntegerField(_("version"), default=1)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    updated_at = models.DateTimeField(_("updated"), auto_now=True)

    class Meta:
        verbose_name = _("earning settings")
        verbose_name_plural = _("earning settings")
        permissions = [
            ("manage_earning_rules", "Can change daily earning limits and the clearance period"),
            ("view_earnings", "Can view real rewards and campaign funding"),
            ("manage_real_rewards", "Can hold, release and reverse real rewards"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(id=1), name="earning_settings_singleton"),
            models.CheckConstraint(condition=Q(clearance_hours__lte=MAX_CLEARANCE_HOURS),
                                   name="earning_settings_clearance_range"),
            models.CheckConstraint(condition=Q(daily_amount_limit__isnull=True) | Q(daily_amount_limit__gt=0),
                                   name="earning_settings_amount_positive"),
            models.CheckConstraint(condition=Q(daily_view_limit__isnull=True) | Q(daily_view_limit__gt=0),
                                   name="earning_settings_views_positive"),
        ]

    def __str__(self):
        return "Earning settings"


class PlanEarningLimit(models.Model):
    """Daily limits for members of one plan. They apply together with the platform limits:
    the lower limit wins, so a plan can never exceed a platform-wide limit."""

    plan = models.OneToOneField("memberships.MembershipPlan", on_delete=models.PROTECT, primary_key=True,
                                related_name="earning_limit")
    daily_amount_limit = AmountField(_("daily earning limit (TZS)"), non_negative=True, null=True, blank=True)
    daily_view_limit = models.PositiveSmallIntegerField(_("daily rewarded views"), null=True, blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    updated_at = models.DateTimeField(_("updated"), auto_now=True)

    class Meta:
        verbose_name = _("plan earning limit")
        constraints = [
            models.CheckConstraint(condition=Q(daily_amount_limit__isnull=True) | Q(daily_amount_limit__gt=0),
                                   name="plan_earning_amount_positive"),
            models.CheckConstraint(condition=Q(daily_view_limit__isnull=True) | Q(daily_view_limit__gt=0),
                                   name="plan_earning_views_positive"),
        ]

    def __str__(self):
        return f"Earning limit for {self.plan_id}"


class CampaignDeposit(BaseModel):
    class Method(models.TextChoices):
        AIRTEL_LIPA = "AIRTEL_LIPA", _("Airtel Lipa")
        BANK_TRANSFER = "BANK_TRANSFER", _("Bank transfer")

    class Status(models.TextChoices):
        RECORDED = "RECORDED", _("Awaiting verification")
        VERIFIED = "VERIFIED", _("Verified")
        REJECTED = "REJECTED", _("Rejected")

    reference = models.CharField(_("deposit reference"), max_length=16, unique=True, default=new_deposit_reference,
                                 editable=False)
    organization = models.ForeignKey("advertising.Organization", on_delete=models.PROTECT, related_name="deposits")
    method = models.CharField(_("payment method"), max_length=16, choices=Method.choices)
    amount = AmountField(_("amount (TZS)"), non_negative=True)
    external_reference = models.CharField(_("transaction reference"), max_length=40)
    reference_key = models.CharField(_("reference key"), max_length=40)
    # "<method>:<key>" while the deposit is open or verified, NULL once rejected: one live use per
    # payment channel (a unique column, enforced the same way on PostgreSQL and MariaDB).
    reference_lock = models.CharField(max_length=60, null=True, blank=True, unique=True, editable=False)
    paid_on = models.DateField(_("date paid"))
    payer_name = models.CharField(_("paid by"), max_length=120, blank=True)
    note = models.CharField(_("supporting information"), max_length=300, blank=True)
    status = models.CharField(_("status"), max_length=10, choices=Status.choices, default=Status.RECORDED)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    decided_at = models.DateTimeField(_("decided"), null=True, blank=True)
    decision_note = models.CharField(_("decision note"), max_length=300, blank=True)
    received_entry = models.OneToOneField("wallets.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                          related_name="+")
    verified_entry = models.OneToOneField("wallets.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                          related_name="+")

    class Meta:
        verbose_name = _("campaign deposit")
        ordering = ["-created_at"]
        permissions = [
            ("record_deposits", "Can record campaign funding deposits"),
            ("verify_deposits", "Can verify the receipt of campaign funding deposits"),
            ("allocate_funding", "Can commit and release campaign funding"),
        ]
        indexes = [models.Index(fields=["organization", "status"], name="earn_deposit_org_idx")]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="earn_deposit_amount_positive"),
            models.CheckConstraint(condition=Q(method__in=["AIRTEL_LIPA", "BANK_TRANSFER"]),
                                   name="earn_deposit_method_valid"),
            models.CheckConstraint(condition=~Q(reference_key=""), name="earn_deposit_reference_present"),
            # Four-eyes: whoever records a deposit can never decide it.
            models.CheckConstraint(condition=Q(decided_by__isnull=True) | ~Q(decided_by=F("recorded_by")),
                                   name="earn_deposit_four_eyes"),
            models.CheckConstraint(
                condition=(Q(status="RECORDED") & Q(decided_by__isnull=True) & Q(received_entry__isnull=True)
                           & Q(verified_entry__isnull=True) & Q(reference_lock__isnull=False))
                | (Q(status="VERIFIED") & Q(decided_by__isnull=False) & Q(decided_at__isnull=False)
                   & Q(received_entry__isnull=False) & Q(verified_entry__isnull=False)
                   & Q(reference_lock__isnull=False))
                | (Q(status="REJECTED") & Q(decided_by__isnull=False) & Q(decided_at__isnull=False)
                   & Q(received_entry__isnull=True) & Q(verified_entry__isnull=True) & Q(reference_lock__isnull=True)
                   & ~Q(decision_note="")),
                name="earn_deposit_state_consistent"),
        ]

    def __str__(self):
        return self.reference


class FundingMovement(BaseModel):
    class Kind(models.TextChoices):
        COMMIT = "COMMIT", _("Committed to the campaign")
        RELEASE = "RELEASE", _("Released from the campaign")

    campaign = models.ForeignKey("advertising.Campaign", on_delete=models.PROTECT, related_name="funding_movements")
    organization = models.ForeignKey("advertising.Organization", on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(_("kind"), max_length=8, choices=Kind.choices)
    amount = AmountField(_("amount (TZS)"), non_negative=True)
    entry = models.OneToOneField("wallets.JournalEntry", on_delete=models.PROTECT, related_name="+")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(_("reason"), max_length=300, blank=True)

    class Meta:
        verbose_name = _("campaign funding movement")
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="earn_movement_amount_positive"),
            models.CheckConstraint(condition=Q(kind__in=["COMMIT", "RELEASE"]), name="earn_movement_kind_valid"),
        ]

    def __str__(self):
        return f"{self.kind} {self.amount}"


class SessionReward(BaseModel):
    class Status(models.TextChoices):
        RESERVED = "RESERVED", _("Offer reserved")
        RELEASED = "RELEASED", _("Not rewarded")
        PENDING = "PENDING", _("Pending")
        AVAILABLE = "AVAILABLE", _("Available")
        REVERSED = "REVERSED", _("Reversed")
        RECOVERY_REQUIRED = "RECOVERY_REQUIRED", _("Reversed: recovery required")

    session = models.OneToOneField("viewing.WatchSession", on_delete=models.PROTECT, related_name="earning")
    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="session_rewards")
    campaign = models.ForeignKey("advertising.Campaign", on_delete=models.PROTECT, related_name="session_rewards")
    policy = models.ForeignKey("rewards.RewardPolicy", on_delete=models.PROTECT, related_name="session_rewards")
    policy_version = models.PositiveIntegerField(_("policy version"))
    amount = AmountField(_("reward (TZS)"), non_negative=True)
    # The member's local (site time zone) day the offer was accepted: daily limits count by it.
    earning_day = models.DateField(_("earning day"))
    status = models.CharField(_("status"), max_length=18, choices=Status.choices, default=Status.RESERVED)
    reasons = models.JSONField(_("reason codes"), default=list, blank=True)
    reserved_at = models.DateTimeField(_("reserved"))
    decided_at = models.DateTimeField(_("decided"), null=True, blank=True)
    clears_at = models.DateTimeField(_("available from"), null=True, blank=True)
    cleared_at = models.DateTimeField(_("cleared"), null=True, blank=True)
    on_hold = models.BooleanField(_("on hold"), default=False)
    hold_reason = models.CharField(_("hold reason"), max_length=300, blank=True)
    reversed_at = models.DateTimeField(_("reversed"), null=True, blank=True)
    reversed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    reversal_reason = models.CharField(_("reversal reason"), max_length=300, blank=True)
    pending_entry = models.OneToOneField("wallets.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="+")
    cleared_entry = models.OneToOneField("wallets.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="+")
    reversal_entry = models.OneToOneField("wallets.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                          related_name="+")

    class Meta:
        verbose_name = _("real reward")
        ordering = ["-reserved_at"]
        indexes = [
            models.Index(fields=["member", "earning_day"], name="earn_reward_member_day_idx"),
            models.Index(fields=["campaign", "status"], name="earn_reward_campaign_idx"),
            models.Index(fields=["status", "clears_at"], name="earn_reward_clearance_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="earn_reward_amount_positive"),
            models.CheckConstraint(
                condition=Q(status__in=["RESERVED", "RELEASED", "PENDING", "AVAILABLE", "REVERSED",
                                        "RECOVERY_REQUIRED"]),
                name="earn_reward_status_valid"),
            models.CheckConstraint(
                condition=(Q(status__in=["RESERVED", "RELEASED"]) & Q(pending_entry__isnull=True)
                           & Q(cleared_entry__isnull=True) & Q(reversal_entry__isnull=True))
                | (Q(status="PENDING") & Q(pending_entry__isnull=False) & Q(clears_at__isnull=False)
                   & Q(cleared_entry__isnull=True) & Q(reversal_entry__isnull=True))
                | (Q(status="AVAILABLE") & Q(pending_entry__isnull=False) & Q(cleared_entry__isnull=False)
                   & Q(cleared_at__isnull=False) & Q(reversal_entry__isnull=True))
                | (Q(status="REVERSED") & Q(pending_entry__isnull=False) & Q(reversal_entry__isnull=False)
                   & Q(reversed_at__isnull=False))
                | (Q(status="RECOVERY_REQUIRED") & Q(cleared_entry__isnull=False) & Q(reversal_entry__isnull=True)
                   & Q(reversed_at__isnull=False)),
                name="earn_reward_state_consistent"),
        ]

    def __str__(self):
        return f"Reward {str(self.pk)[:8]}"

    @property
    def counts_toward_limits(self):
        return self.status in (self.Status.RESERVED, self.Status.PENDING, self.Status.AVAILABLE,
                               self.Status.RECOVERY_REQUIRED)


ACTIVE_REWARD_STATES = ("RESERVED", "PENDING", "AVAILABLE", "RECOVERY_REQUIRED")
