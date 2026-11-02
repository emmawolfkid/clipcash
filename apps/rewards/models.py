"""
Demonstration rewards (Phase 07). Use apps.rewards.services.

EVERYTHING HERE IS SIMULATED. Demo rewards are records of what a verified view
*would* earn under an approved policy. They are not money, not balances, not
obligations and not payable: they never touch apps.wallets (the ledger), and
there is no field or flag that turns them into real rewards. Real rewards need
reconciled advertiser funding (Phase 08) and a separate, reviewed implementation.

* RewardPolicy: per campaign and approved campaign revision, versioned,
  four-eyes approval; approved versions are immutable.
* DemoReward: exactly one decision per verified watch session.
* CampaignRewardBudget: simulated capacity consumed by qualified demo rewards,
  reserved and released under a row lock.

PostgreSQL guards: migration 0002.
"""

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _

from apps.core.fields import AmountField
from apps.core.models import BaseModel
from apps.core.money import DEFAULT_CURRENCY


class RewardPolicy(BaseModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", _("Draft")
        APPROVED = "APPROVED", _("Approved")
        REJECTED = "REJECTED", _("Rejected")
        RETIRED = "RETIRED", _("Retired")

    campaign = models.ForeignKey("advertising.Campaign", on_delete=models.PROTECT, related_name="reward_policies")
    campaign_revision = models.PositiveIntegerField(_("approved campaign revision"))
    version = models.PositiveIntegerField(_("version"))
    reward_amount = AmountField(_("demo reward per verified view (TZS)"), non_negative=True)
    currency = models.CharField(_("currency"), max_length=3, default=DEFAULT_CURRENCY)
    status = models.CharField(_("status"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    notes = models.CharField(_("notes"), max_length=300, blank=True)
    effective_from = models.DateTimeField(_("effective from"), null=True, blank=True)
    effective_until = models.DateTimeField(_("effective until"), null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    decided_at = models.DateTimeField(_("decided"), null=True, blank=True)
    decision_reason = models.CharField(_("decision reason"), max_length=300, blank=True)

    class Meta:
        verbose_name = _("demo reward policy")
        verbose_name_plural = _("demo reward policies")
        ordering = ["campaign", "-version"]
        permissions = [
            ("manage_reward_policies", "Can draft demo reward policies"),
            ("approve_reward_policies", "Can approve demo reward policies"),
            ("manage_demo_rewards", "Can evaluate and reverse demo rewards"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["campaign", "version"], name="reward_policy_version_unique"),
            models.UniqueConstraint(fields=["campaign"], condition=Q(status="APPROVED"),
                                    name="reward_policy_one_approved"),
            models.CheckConstraint(condition=Q(reward_amount__gt=0), name="reward_policy_amount_positive"),
            models.CheckConstraint(condition=Q(currency="TZS"), name="reward_policy_currency_supported"),
            models.CheckConstraint(condition=Q(status__in=["DRAFT", "APPROVED", "REJECTED", "RETIRED"]),
                                   name="reward_policy_status_valid"),
            models.CheckConstraint(
                condition=Q(status__in=["DRAFT", "REJECTED"])
                | (Q(effective_from__isnull=False) & Q(decided_by__isnull=False) & Q(decided_at__isnull=False)),
                name="reward_policy_approval_recorded"),
            models.CheckConstraint(condition=~Q(status="RETIRED") | Q(effective_until__isnull=False),
                                   name="reward_policy_retired_has_end"),
            models.CheckConstraint(condition=Q(effective_until__isnull=True)
                                   | Q(effective_until__gte=F("effective_from")),
                                   name="reward_policy_dates_ordered"),
            # Four-eyes: the approver is never the author.
            models.CheckConstraint(condition=Q(decided_by__isnull=True) | ~Q(decided_by=F("created_by")),
                                   name="reward_policy_four_eyes"),
        ]

    def __str__(self):
        return f"Demo reward policy v{self.version}"


class DemoReward(BaseModel):
    class Status(models.TextChoices):
        QUALIFIED = "DEMO_QUALIFIED", _("Simulated reward recorded")
        REVERSED = "DEMO_REVERSED", _("Simulated reward reversed")
        REJECTED = "DEMO_REJECTED", _("Not eligible")

    # One reward decision per verified watch session, ever.
    session = models.OneToOneField("viewing.WatchSession", on_delete=models.PROTECT, related_name="demo_reward")
    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="demo_rewards")
    campaign = models.ForeignKey("advertising.Campaign", on_delete=models.PROTECT, related_name="demo_rewards")
    campaign_revision = models.PositiveIntegerField(_("campaign revision"))
    policy = models.ForeignKey(RewardPolicy, null=True, blank=True, on_delete=models.PROTECT,
                               related_name="demo_rewards")
    policy_version = models.PositiveIntegerField(_("policy version"), null=True, blank=True)
    amount = AmountField(_("simulated amount (TZS)"), non_negative=True)
    currency = models.CharField(_("currency"), max_length=3, default=DEFAULT_CURRENCY)
    status = models.CharField(_("status"), max_length=16, choices=Status.choices)
    reasons = models.JSONField(_("reason codes"), default=list, blank=True)
    decided_at = models.DateTimeField(_("decided"))
    reversed_at = models.DateTimeField(_("reversed"), null=True, blank=True)
    reversed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    reversal_reason = models.CharField(_("reversal reason"), max_length=300, blank=True)

    class Meta:
        verbose_name = _("demo reward")
        ordering = ["-decided_at"]
        indexes = [
            models.Index(fields=["member", "-decided_at"], name="demo_reward_member_idx"),
            models.Index(fields=["campaign", "status"], name="demo_reward_campaign_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(status__in=["DEMO_QUALIFIED", "DEMO_REVERSED", "DEMO_REJECTED"]),
                                   name="demo_reward_status_valid"),
            models.CheckConstraint(condition=Q(currency="TZS"), name="demo_reward_currency_supported"),
            # Qualified/reversed rewards carry a policy and a positive amount; rejections carry nothing.
            models.CheckConstraint(
                condition=(Q(status="DEMO_REJECTED") & Q(amount=0))
                | (~Q(status="DEMO_REJECTED") & Q(amount__gt=0) & Q(policy__isnull=False)
                   & Q(policy_version__isnull=False)),
                name="demo_reward_amount_consistent"),
            models.CheckConstraint(
                condition=(Q(status="DEMO_REVERSED") & Q(reversed_at__isnull=False))
                | (~Q(status="DEMO_REVERSED") & Q(reversed_at__isnull=True)),
                name="demo_reward_reversal_recorded"),
        ]

    def __str__(self):
        return f"Demo reward {str(self.pk)[:8]}"

    @property
    def counts(self):
        return self.status == self.Status.QUALIFIED


class CampaignRewardBudget(models.Model):
    """Simulated capacity used by qualified demo rewards. Not money."""

    campaign = models.OneToOneField("advertising.Campaign", on_delete=models.PROTECT, primary_key=True,
                                    related_name="reward_budget")
    demo_consumed = AmountField(_("demo consumed (TZS, simulated)"), non_negative=True, default=0)
    updated_at = models.DateTimeField(_("updated"), auto_now=True)

    class Meta:
        verbose_name = _("campaign demo reward budget")
        constraints = [models.CheckConstraint(condition=Q(demo_consumed__gte=0), name="reward_budget_non_negative")]

    def __str__(self):
        return f"Demo budget usage for {self.campaign_id}"
