"""
REF-01: single-level referral programme. Use apps.referrals.services and apps.referrals.funding.

* ReferralCode: one unique, random code per member (immutable).
* ReferralPolicy: versioned programme rules. A change is PROPOSED by one staff member and becomes
  ACTIVE only when a different person approves it (database-enforced); approved terms never change.
  Each referral keeps the version in force when it was registered: its qualification rules and the
  MAXIMUM reward. A later version can lower an unpaid reward (the lower amount applies when it is
  allocated) but never raise it, so a rule change cannot silently increase what was promised.
* Referral: exactly one referrer per referred member, recorded only at registration (database-
  enforced: never retroactively, never self or circular) and immutable afterwards. Status:
  AWAITING -> UNDER_REVIEW | QUALIFIED | REWARD_PENDING | EXPIRED | REJECTED; UNDER_REVIEW ->
  QUALIFIED | REWARD_PENDING | REJECTED; QUALIFIED -> REWARD_PENDING | REJECTED; REWARD_PENDING ->
  REWARD_CLEARED | REVERSED; REWARD_CLEARED -> REVERSED.
* ReferralReward: at most one per referral, paid from the referral programme's verified marketing
  budget into the referrer's EXISTING wallet (pending, then available). Never above the referral's
  policy amount (database-enforced).
* MarketingDeposit / ReferralFundingMovement: ClipCash's marketing budget: recorded, verified by a
  different person, then committed to (or released from) the programme. One ledger entry each.

No balance is stored here: money only moves through apps.wallets.services (the double-entry ledger).
Database guards: migration 0002.
"""

import secrets

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _

from apps.core.fields import AmountField
from apps.core.models import BaseModel

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # No 0/O or 1/I: easy to read aloud and type.
CODE_LENGTH = 8
MAX_REWARD = 1_000_000
MAX_CLEARANCE_HOURS = 720


def new_referral_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _unused in range(CODE_LENGTH))


def new_referral_reference():
    return "RF-" + secrets.token_hex(4).upper()


def new_marketing_reference():
    return "MKT-" + secrets.token_hex(4).upper()


class ReferralCode(BaseModel):
    member = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="referral_code")
    code = models.CharField(_("referral code"), max_length=12, unique=True, default=new_referral_code, editable=False)

    class Meta:
        verbose_name = _("referral code")
        constraints = [models.CheckConstraint(condition=Q(code__regex=r"^[A-Z2-9]{8}$"), name="ref_code_format")]

    def __str__(self):
        return self.code


class ReferralPolicy(models.Model):
    class Status(models.TextChoices):
        PROPOSED = "PROPOSED", _("Awaiting approval")
        ACTIVE = "ACTIVE", _("Approved")
        REJECTED = "REJECTED", _("Not approved")

    id = models.BigAutoField(primary_key=True)
    version = models.PositiveIntegerField(_("version"), unique=True)
    base_version = models.PositiveIntegerField(_("based on version"), null=True, blank=True)
    status = models.CharField(_("status"), max_length=10, choices=Status.choices, default=Status.PROPOSED)
    tracking_open = models.BooleanField(_("record new referrals"), default=True)
    rewards_open = models.BooleanField(_("offer referral rewards"), default=False)
    reward_amount = AmountField(_("reward per qualified referral (TZS)"), non_negative=True, default=0)
    referrer_requires_membership = models.BooleanField(_("referrer needs an active membership"), default=True)
    referred_requires_membership = models.BooleanField(_("new member needs an active membership"), default=False)
    min_account_age_days = models.PositiveSmallIntegerField(_("minimum account age (days)"), default=7)
    required_verified_views = models.PositiveSmallIntegerField(_("verified sponsored views required"), default=5)
    required_view_days = models.PositiveSmallIntegerField(_("different days with verified views"), default=3)
    qualification_days = models.PositiveSmallIntegerField(_("qualification period (days)"), default=30)
    daily_reward_limit = models.PositiveSmallIntegerField(_("rewards per referrer per day"), null=True, blank=True)
    lifetime_reward_limit = models.PositiveIntegerField(_("rewards per referrer in total"), null=True, blank=True)
    clearance_hours = models.PositiveSmallIntegerField(_("clearance period (hours)"), default=168)
    reason = models.CharField(_("reason for change"), max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    created_at = models.DateTimeField(_("proposed"), auto_now_add=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    decided_at = models.DateTimeField(_("decided"), null=True, blank=True)
    decision_note = models.CharField(_("decision note"), max_length=300, blank=True)

    class Meta:
        verbose_name = _("referral policy version")
        ordering = ["-version"]
        permissions = [
            ("view_referrals", "Can view the referral programme"),
            ("manage_referral_policy", "Can propose referral programme rules"),
            ("approve_referral_policy", "Can approve referral programme rules"),
            ("review_referrals", "Can review, reject and reverse referrals"),
            ("record_marketing_deposits", "Can record marketing budget deposits"),
            ("verify_marketing_deposits", "Can verify marketing budget deposits"),
            ("allocate_referral_budget", "Can commit and release the referral budget"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(status__in=["PROPOSED", "ACTIVE", "REJECTED"]),
                                   name="ref_policy_status_valid"),
            models.CheckConstraint(condition=Q(reward_amount__gte=0) & Q(reward_amount__lte=MAX_REWARD),
                                   name="ref_policy_amount_range"),
            models.CheckConstraint(condition=Q(rewards_open=False) | Q(reward_amount__gt=0),
                                   name="ref_policy_open_needs_amount"),
            models.CheckConstraint(condition=Q(clearance_hours__lte=MAX_CLEARANCE_HOURS),
                                   name="ref_policy_clearance_range"),
            models.CheckConstraint(
                condition=Q(required_verified_views__gte=1) & Q(required_verified_views__lte=500)
                & Q(required_view_days__gte=1) & Q(required_view_days__lte=F("required_verified_views"))
                & Q(qualification_days__gte=1) & Q(qualification_days__lte=365) & Q(min_account_age_days__lte=365)
                & Q(required_view_days__lte=F("qualification_days")),
                name="ref_policy_qualification_range"),
            models.CheckConstraint(condition=(Q(daily_reward_limit__isnull=True) | Q(daily_reward_limit__gt=0))
                                   & (Q(lifetime_reward_limit__isnull=True) | Q(lifetime_reward_limit__gt=0)),
                                   name="ref_policy_limits_positive"),
            # Four-eyes: whoever proposes a version can never decide it.
            models.CheckConstraint(condition=Q(decided_by__isnull=True) | ~Q(decided_by=F("created_by")),
                                   name="ref_policy_four_eyes"),
            # Only the built-in first version (which offers no money) is active without an approver.
            models.CheckConstraint(
                condition=Q(status="PROPOSED", decided_by__isnull=True, decided_at__isnull=True)
                | Q(status__in=["ACTIVE", "REJECTED"], decided_by__isnull=False, decided_at__isnull=False)
                | Q(status="ACTIVE", version=1, decided_by__isnull=True, rewards_open=False, reward_amount=0),
                name="ref_policy_decision_recorded"),
        ]

    def __str__(self):
        return f"Referral policy v{self.version}"


class Referral(BaseModel):
    class Source(models.TextChoices):
        LINK = "LINK", _("Referral link")
        CODE = "CODE", _("Code entered")

    class Status(models.TextChoices):
        AWAITING = "AWAITING", _("Registered: awaiting qualification")
        UNDER_REVIEW = "UNDER_REVIEW", _("Under review")
        QUALIFIED = "QUALIFIED", _("Qualified")
        REWARD_PENDING = "REWARD_PENDING", _("Reward pending")
        REWARD_CLEARED = "REWARD_CLEARED", _("Reward cleared")
        REJECTED = "REJECTED", _("Rejected")
        REVERSED = "REVERSED", _("Reversed")
        EXPIRED = "EXPIRED", _("Did not qualify in time")

    reference = models.CharField(_("reference"), max_length=12, unique=True, default=new_referral_reference,
                                 editable=False)
    referrer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="referrals_made")
    referred = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="referral")
    code = models.CharField(_("code used"), max_length=12)
    source = models.CharField(_("source"), max_length=4, choices=Source.choices)
    policy = models.ForeignKey(ReferralPolicy, on_delete=models.PROTECT, related_name="referrals")
    status = models.CharField(_("status"), max_length=16, choices=Status.choices, default=Status.AWAITING)
    # Why there is no reward (yet), or why it was rejected or reversed: a code with a member-facing text.
    outcome_reason = models.CharField(_("reason code"), max_length=40, blank=True)
    risk_flags = models.JSONField(_("risk signals"), default=list, blank=True)
    qualified_at = models.DateTimeField(_("qualified"), null=True, blank=True)
    decided_at = models.DateTimeField(_("decided"), null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    review_note = models.CharField(_("review note"), max_length=300, blank=True)

    class Meta:
        verbose_name = _("referral")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["referrer", "status"], name="ref_referrer_status_idx"),
            models.Index(fields=["status", "created_at"], name="ref_status_created_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=~Q(referrer=F("referred")), name="ref_not_self"),
            models.CheckConstraint(condition=Q(source__in=["LINK", "CODE"]), name="ref_source_valid"),
            models.CheckConstraint(
                condition=Q(status__in=["AWAITING", "UNDER_REVIEW", "QUALIFIED", "REWARD_PENDING", "REWARD_CLEARED",
                                        "REJECTED", "REVERSED", "EXPIRED"]), name="ref_status_valid"),
            models.CheckConstraint(condition=Q(reviewed_by__isnull=True)
                                   | (~Q(reviewed_by=F("referrer")) & ~Q(reviewed_by=F("referred"))),
                                   name="ref_not_reviewed_by_party"),
            models.CheckConstraint(condition=Q(status__in=["AWAITING", "UNDER_REVIEW", "REJECTED", "EXPIRED"])
                                   | Q(qualified_at__isnull=False), name="ref_qualified_recorded"),
        ]

    def __str__(self):
        return self.reference


class ReferralReward(BaseModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        AVAILABLE = "AVAILABLE", _("Available")
        REVERSED = "REVERSED", _("Reversed")
        RECOVERY_REQUIRED = "RECOVERY_REQUIRED", _("Reversed: recovery required")

    referral = models.OneToOneField(Referral, on_delete=models.PROTECT, related_name="reward")
    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="referral_rewards")
    policy_version = models.PositiveIntegerField(_("policy version"))
    amount = AmountField(_("reward (TZS)"), non_negative=True)
    reward_day = models.DateField(_("reward day"))  # Site-local day; daily limits count by it.
    status = models.CharField(_("status"), max_length=18, choices=Status.choices, default=Status.PENDING)
    clears_at = models.DateTimeField(_("available from"))
    cleared_at = models.DateTimeField(_("cleared"), null=True, blank=True)
    on_hold = models.BooleanField(_("on hold"), default=False)
    hold_reason = models.CharField(_("hold reason"), max_length=300, blank=True)
    reversed_at = models.DateTimeField(_("reversed"), null=True, blank=True)
    reversed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    reversal_reason = models.CharField(_("reversal reason"), max_length=300, blank=True)
    pending_entry = models.OneToOneField("wallets.JournalEntry", on_delete=models.PROTECT, related_name="+")
    cleared_entry = models.OneToOneField("wallets.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="+")
    reversal_entry = models.OneToOneField("wallets.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                          related_name="+")

    class Meta:
        verbose_name = _("referral reward")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["member", "reward_day"], name="ref_reward_member_day_idx"),
            models.Index(fields=["status", "clears_at"], name="ref_reward_clearance_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0) & Q(amount__lte=MAX_REWARD),
                                   name="ref_reward_amount_range"),
            models.CheckConstraint(
                condition=(Q(status="PENDING") & Q(cleared_entry__isnull=True) & Q(reversal_entry__isnull=True))
                | (Q(status="AVAILABLE") & Q(cleared_entry__isnull=False) & Q(cleared_at__isnull=False)
                   & Q(reversal_entry__isnull=True))
                | (Q(status="REVERSED") & Q(reversal_entry__isnull=False) & Q(reversed_at__isnull=False))
                | (Q(status="RECOVERY_REQUIRED") & Q(cleared_entry__isnull=False) & Q(reversal_entry__isnull=True)
                   & Q(reversed_at__isnull=False)),
                name="ref_reward_state_consistent"),
        ]

    def __str__(self):
        return f"Referral reward {str(self.pk)[:8]}"


ACTIVE_REWARD_STATES = ("PENDING", "AVAILABLE", "RECOVERY_REQUIRED")


class MarketingDeposit(BaseModel):
    class Method(models.TextChoices):
        AIRTEL_LIPA = "AIRTEL_LIPA", _("Airtel Lipa")
        BANK_TRANSFER = "BANK_TRANSFER", _("Bank transfer")

    class Status(models.TextChoices):
        RECORDED = "RECORDED", _("Awaiting verification")
        VERIFIED = "VERIFIED", _("Verified")
        REJECTED = "REJECTED", _("Rejected")

    reference = models.CharField(_("deposit reference"), max_length=16, unique=True, default=new_marketing_reference,
                                 editable=False)
    method = models.CharField(_("payment method"), max_length=16, choices=Method.choices)
    amount = AmountField(_("amount (TZS)"), non_negative=True)
    external_reference = models.CharField(_("transaction reference"), max_length=40)
    reference_key = models.CharField(_("reference key"), max_length=40)
    # "<method>:<key>" while open or verified, NULL once rejected: one live use per payment channel.
    reference_lock = models.CharField(max_length=60, null=True, blank=True, unique=True, editable=False)
    paid_on = models.DateField(_("date paid"))
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
        verbose_name = _("marketing budget deposit")
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="ref_deposit_amount_positive"),
            models.CheckConstraint(condition=Q(method__in=["AIRTEL_LIPA", "BANK_TRANSFER"]),
                                   name="ref_deposit_method_valid"),
            models.CheckConstraint(condition=~Q(reference_key=""), name="ref_deposit_reference_present"),
            models.CheckConstraint(condition=Q(decided_by__isnull=True) | ~Q(decided_by=F("recorded_by")),
                                   name="ref_deposit_four_eyes"),
            models.CheckConstraint(
                condition=(Q(status="RECORDED") & Q(decided_by__isnull=True) & Q(received_entry__isnull=True)
                           & Q(verified_entry__isnull=True) & Q(reference_lock__isnull=False))
                | (Q(status="VERIFIED") & Q(decided_by__isnull=False) & Q(decided_at__isnull=False)
                   & Q(received_entry__isnull=False) & Q(verified_entry__isnull=False)
                   & Q(reference_lock__isnull=False))
                | (Q(status="REJECTED") & Q(decided_by__isnull=False) & Q(decided_at__isnull=False)
                   & Q(received_entry__isnull=True) & Q(verified_entry__isnull=True) & Q(reference_lock__isnull=True)
                   & ~Q(decision_note="")),
                name="ref_deposit_state_consistent"),
        ]

    def __str__(self):
        return self.reference


class ReferralFundingMovement(BaseModel):
    class Kind(models.TextChoices):
        COMMIT = "COMMIT", _("Committed to referral rewards")
        RELEASE = "RELEASE", _("Released to the marketing budget")

    kind = models.CharField(_("kind"), max_length=8, choices=Kind.choices)
    amount = AmountField(_("amount (TZS)"), non_negative=True)
    entry = models.OneToOneField("wallets.JournalEntry", on_delete=models.PROTECT, related_name="+")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(_("reason"), max_length=300, blank=True)

    class Meta:
        verbose_name = _("referral budget movement")
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="ref_movement_amount_positive"),
            models.CheckConstraint(condition=Q(kind__in=["COMMIT", "RELEASE"]), name="ref_movement_kind_valid"),
        ]

    def __str__(self):
        return f"{self.kind} {self.amount}"
