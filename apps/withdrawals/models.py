"""
Withdrawals and payouts (Phase 10), DEMONSTRATION ONLY. Use apps.withdrawals.services.

* PayoutBeneficiary: member-owned destination. Data minimization: the full
  phone or account number is validated and then DISCARDED. Only the last four
  digits (for display) and a keyed fingerprint (for duplicate/change detection)
  are stored, so nothing sensitive can leak. Real payouts will need provider
  tokenization or encrypted storage (docs/architecture/withdrawals.md).
* WithdrawalRequest: the future REAL request. The database refuses every insert
  in this phase (migration 0002); eligibility always denies.
* DemoWithdrawal: a separate, staff-run SIMULATION with its own DEMO_* states.
  Different table, so a demo can never become a real payable request.
* SandboxPayout: the simulated payout provider's own records.
"""

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _

from apps.core.fields import AmountField
from apps.core.models import BaseModel
from apps.core.money import DEFAULT_CURRENCY


class PayoutBeneficiary(BaseModel):
    class Kind(models.TextChoices):
        MOBILE_MONEY = "MOBILE_MONEY", _("Mobile money")
        BANK = "BANK", _("Bank account")

    class Network(models.TextChoices):
        MPESA = "MPESA", _("Vodacom M-Pesa")
        AIRTEL = "AIRTEL", _("Airtel Money")
        MIXX = "MIXX", _("Mixx by Yas")
        HALOPESA = "HALOPESA", _("Halotel HaloPesa")

    class Verification(models.TextChoices):
        UNVERIFIED = "UNVERIFIED", _("Not verified")
        DEMO_VERIFIED = "DEMO_VERIFIED", _("Demo check passed (not a real verification)")

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", _("Active")
        DEACTIVATED = "DEACTIVATED", _("Removed")

    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payout_beneficiaries")
    kind = models.CharField(_("type"), max_length=14, choices=Kind.choices)
    network = models.CharField(_("mobile network"), max_length=10, choices=Network.choices, blank=True)
    bank_code = models.CharField(_("bank code"), max_length=12, blank=True)
    holder_name = models.CharField(_("account holder name"), max_length=100)
    identifier_last4 = models.CharField(_("last four digits"), max_length=4)
    identifier_fingerprint = models.CharField(_("fingerprint"), max_length=64)
    verification = models.CharField(_("verification"), max_length=14, choices=Verification.choices,
                                    default=Verification.UNVERIFIED)
    verified_at = models.DateTimeField(_("checked"), null=True, blank=True)
    status = models.CharField(_("status"), max_length=12, choices=Status.choices, default=Status.ACTIVE)
    deactivated_at = models.DateTimeField(_("removed"), null=True, blank=True)

    class Meta:
        verbose_name = _("payout beneficiary")
        ordering = ["-created_at"]
        permissions = [
            ("view_withdrawals", "Can view withdrawals and demo payouts"),
            ("review_withdrawals", "Can review and approve demo withdrawals"),
            ("operate_payout_sandbox", "Can run the demo payout simulator"),
            ("view_beneficiaries", "Can inspect masked payout beneficiaries"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(Q(kind="MOBILE_MONEY") & ~Q(network="") & Q(bank_code=""))
                | (Q(kind="BANK") & Q(network="") & Q(bank_code__regex=r"^[A-Z0-9]{2,12}$")),
                name="beneficiary_kind_fields"),
            models.CheckConstraint(condition=Q(identifier_last4__regex=r"^[0-9]{4}$"), name="beneficiary_last4_digits"),
            models.CheckConstraint(condition=Q(identifier_fingerprint__regex=r"^[0-9a-f]{64}$"),
                                   name="beneficiary_fingerprint_format"),
            models.CheckConstraint(condition=~Q(holder_name=""), name="beneficiary_holder_name"),
            models.UniqueConstraint(fields=["member", "kind", "identifier_fingerprint"],
                                    condition=Q(status="ACTIVE"), name="beneficiary_active_unique"),
            models.CheckConstraint(condition=(Q(status="ACTIVE") & Q(deactivated_at__isnull=True))
                                   | (Q(status="DEACTIVATED") & Q(deactivated_at__isnull=False)),
                                   name="beneficiary_deactivation_recorded"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} •••• {self.identifier_last4}"

    @property
    def destination_label(self):
        provider = self.get_network_display() if self.kind == self.Kind.MOBILE_MONEY else self.bank_code
        return f"{provider} •••• {self.identifier_last4}"


class WithdrawalRequest(BaseModel):
    """FUTURE real withdrawal request. Inserts are refused by the database in Phase 10."""

    class Status(models.TextChoices):
        REQUESTED = "REQUESTED", _("Requested")
        UNDER_REVIEW = "UNDER_REVIEW", _("Under review")
        APPROVED = "APPROVED", _("Approved")
        PAYOUT_PENDING = "PAYOUT_PENDING", _("Payout pending")
        PAID = "PAID", _("Paid")
        FAILED = "FAILED", _("Failed")
        REJECTED = "REJECTED", _("Rejected")
        CANCELLED = "CANCELLED", _("Cancelled")

    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="withdrawal_requests")
    wallet = models.ForeignKey("wallets.Wallet", on_delete=models.PROTECT, related_name="withdrawal_requests")
    beneficiary = models.ForeignKey(PayoutBeneficiary, on_delete=models.PROTECT, related_name="+")
    amount = AmountField(_("amount (TZS)"), non_negative=True)
    currency = models.CharField(_("currency"), max_length=3, default=DEFAULT_CURRENCY)
    status = models.CharField(_("status"), max_length=16, choices=Status.choices, default=Status.REQUESTED)
    idempotency_key = models.CharField(_("idempotency key"), max_length=64, unique=True)

    class Meta:
        verbose_name = _("withdrawal request (real, locked)")
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="withdrawal_amount_positive")]


class DemoWithdrawal(BaseModel):
    """A SIMULATED withdrawal scenario run by staff. Never a payment obligation."""

    class Status(models.TextChoices):
        REQUESTED = "DEMO_REQUESTED", _("Demo: requested")
        UNDER_REVIEW = "DEMO_UNDER_REVIEW", _("Demo: under review")
        APPROVED = "DEMO_APPROVED", _("Demo: approved")
        PAYOUT_PENDING = "DEMO_PAYOUT_PENDING", _("Demo: payout pending")
        UNCERTAIN = "DEMO_UNCERTAIN", _("Demo: payout status unknown")
        PAID = "DEMO_PAID", _("Demo: paid (simulated)")
        FAILED = "DEMO_FAILED", _("Demo: payout failed")
        RETURNED = "DEMO_RETURNED", _("Demo: payout returned")
        REJECTED = "DEMO_REJECTED", _("Demo: rejected")
        CANCELLED = "DEMO_CANCELLED", _("Demo: cancelled")
        EXPIRED = "DEMO_EXPIRED", _("Demo: expired")

    reference = models.CharField(_("demo reference"), max_length=20, unique=True)
    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="demo_withdrawals")
    beneficiary = models.ForeignKey(PayoutBeneficiary, on_delete=models.PROTECT, related_name="demo_withdrawals")
    # Snapshot: later beneficiary changes never alter this request's destination.
    destination_kind = models.CharField(max_length=14)
    destination_provider = models.CharField(max_length=40)
    destination_last4 = models.CharField(max_length=4)
    destination_fingerprint = models.CharField(max_length=64)
    destination_holder = models.CharField(max_length=100)
    simulated_amount = AmountField(_("simulated amount (TZS)"), non_negative=True)
    currency = models.CharField(_("currency"), max_length=3, default=DEFAULT_CURRENCY)
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, default=Status.REQUESTED)
    high_risk = models.BooleanField(_("needs two approvers"), default=False)
    flags = models.JSONField(_("risk flags"), default=list, blank=True)
    reason_code = models.CharField(_("reason code"), max_length=40, blank=True)
    idempotency_key = models.CharField(_("idempotency key"), max_length=64)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    second_approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                           on_delete=models.PROTECT, related_name="+")
    payout_initiated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                            on_delete=models.PROTECT, related_name="+")
    payout_reference = models.CharField(_("simulated payout reference"), max_length=32, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    payout_started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("demo withdrawal (simulated)")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "-created_at"], name="demo_withdrawal_status_idx")]
        constraints = [
            models.CheckConstraint(condition=Q(simulated_amount__gt=0), name="demo_withdrawal_amount_positive"),
            models.CheckConstraint(condition=Q(currency="TZS"), name="demo_withdrawal_currency"),
            models.UniqueConstraint(fields=["created_by", "idempotency_key"], name="demo_withdrawal_idempotency"),
            models.UniqueConstraint(fields=["payout_reference"], condition=~Q(payout_reference=""),
                                    name="demo_withdrawal_payout_reference_unique"),
            models.UniqueConstraint(fields=["member"], condition=Q(status__in=[
                "DEMO_REQUESTED", "DEMO_UNDER_REVIEW", "DEMO_APPROVED", "DEMO_PAYOUT_PENDING", "DEMO_UNCERTAIN"]),
                name="demo_withdrawal_one_open_per_member"),
            # Separation of duties.
            models.CheckConstraint(condition=Q(approved_by__isnull=True) | ~Q(approved_by=F("created_by")),
                                   name="demo_withdrawal_no_self_approval"),
            models.CheckConstraint(condition=Q(second_approved_by__isnull=True)
                                   | (~Q(second_approved_by=F("created_by")) & ~Q(second_approved_by=F("approved_by"))),
                                   name="demo_withdrawal_distinct_second_approver"),
            models.CheckConstraint(condition=Q(payout_initiated_by__isnull=True)
                                   | (~Q(payout_initiated_by=F("approved_by"))
                                      & (Q(second_approved_by__isnull=True)
                                         | ~Q(payout_initiated_by=F("second_approved_by")))),
                                   name="demo_withdrawal_approver_not_executor"),
            models.CheckConstraint(condition=~Q(status__in=["DEMO_APPROVED", "DEMO_PAYOUT_PENDING", "DEMO_UNCERTAIN",
                                                            "DEMO_PAID", "DEMO_FAILED", "DEMO_RETURNED"])
                                   | (Q(approved_by__isnull=False)
                                      & (Q(high_risk=False) | Q(second_approved_by__isnull=False))),
                                   name="demo_withdrawal_approval_required"),
            models.CheckConstraint(condition=~Q(status__in=["DEMO_PAYOUT_PENDING", "DEMO_UNCERTAIN", "DEMO_PAID",
                                                            "DEMO_FAILED", "DEMO_RETURNED"])
                                   | (~Q(payout_reference="") & Q(payout_initiated_by__isnull=False)),
                                   name="demo_withdrawal_payout_recorded"),
        ]

    def __str__(self):
        return self.reference


class DemoWithdrawalDecision(models.Model):
    """Immutable decision history of a demo withdrawal."""

    id = models.BigAutoField(primary_key=True)
    withdrawal = models.ForeignKey(DemoWithdrawal, on_delete=models.PROTECT, related_name="decisions")
    action = models.CharField(max_length=30)
    from_status = models.CharField(max_length=20)
    to_status = models.CharField(max_length=20)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                              related_name="+")
    reason_code = models.CharField(max_length=40, blank=True)
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("demo withdrawal decision")
        ordering = ["created_at", "id"]


class SandboxPayout(models.Model):
    """The SIMULATED payout provider's own records (changed only by the simulator)."""

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        SUCCEEDED = "SUCCEEDED", _("Succeeded")
        FAILED = "FAILED", _("Failed")
        RETURNED = "RETURNED", _("Returned")

    reference = models.CharField(max_length=32, primary_key=True)
    withdrawal_reference = models.CharField(max_length=20)
    amount = AmountField(non_negative=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    unreachable = models.BooleanField(default=False)  # Simulates a provider timeout/outage.
    sequence = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("sandbox payout (simulated)")


class PayoutEvent(models.Model):
    """Append-only provider notices and status checks for demo payouts (redacted)."""

    id = models.BigAutoField(primary_key=True)
    provider = models.CharField(max_length=20)
    event_id = models.CharField(max_length=100)
    authenticated = models.BooleanField(default=False)
    outcome = models.CharField(max_length=12)
    payout_reference = models.CharField(max_length=32, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("payout event")
        constraints = [models.UniqueConstraint(fields=["provider", "event_id"], condition=Q(authenticated=True),
                                               name="payout_event_once")]


class WithdrawalException(BaseModel):
    class Status(models.TextChoices):
        OPEN = "OPEN", _("Open")
        RESOLVED = "RESOLVED", _("Resolved")

    withdrawal = models.ForeignKey(DemoWithdrawal, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="exceptions")
    code = models.CharField(max_length=40)
    detail = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.CharField(max_length=300, blank=True)

    class Meta:
        verbose_name = _("withdrawal exception")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["withdrawal", "code"], condition=Q(status="OPEN"),
                                    name="withdrawal_exception_open_unique", nulls_distinct=False),
            models.CheckConstraint(condition=Q(status="OPEN") | (Q(resolved_by__isnull=False)
                                                                 & Q(resolved_at__isnull=False)),
                                   name="withdrawal_exception_resolution_recorded"),
        ]
