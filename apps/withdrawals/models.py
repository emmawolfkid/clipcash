"""
Withdrawals and payouts. Use apps.withdrawals.services (destinations, demo) and
apps.withdrawals.real (FIN-02 real withdrawals).

* PayoutBeneficiary: member-owned destination. The last four digits (display) and a
  keyed fingerprint (duplicate detection) are stored in clear. FIN-02: the full phone
  or account number is kept only ENCRYPTED (apps.withdrawals.protect, key from the
  environment) so a payout operator can send money; it is shown only to the operator
  who claimed the payout, and every reveal is audited.
* WithdrawalRequest: a REAL request (FIN-02, switched off by default), with its settings
  version, fee and an immutable destination snapshot. Database guards: migration 0003.
* DemoWithdrawal: a separate, staff-run SIMULATION with its own DEMO_* states.
  Different table, so a demo can never become a real payable request.
* SandboxPayout: the simulated payout provider's own records.
"""

import secrets

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
    bank_name = models.CharField(_("bank"), max_length=100, blank=True)
    holder_name = models.CharField(_("account holder name"), max_length=100)
    identifier_last4 = models.CharField(_("last four digits"), max_length=4)
    identifier_fingerprint = models.CharField(_("fingerprint"), max_length=64)
    # FIN-02: the full number, encrypted. Empty for destinations saved before FIN-02 (they must be
    # added again before they can receive a real payout).
    identifier_encrypted = models.TextField(_("number (encrypted)"), blank=True, editable=False)
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
        provider = self.get_network_display() if self.kind == self.Kind.MOBILE_MONEY else (self.bank_name
                                                                                             or self.bank_code)
        return f"{provider} •••• {self.identifier_last4}"

    @property
    def payable(self):
        """FIN-02: can receive a real payout (the full number is stored, encrypted)."""
        return bool(self.identifier_encrypted)


def new_withdrawal_reference():
    return "WD-" + secrets.token_hex(4).upper()


class WithdrawalSettings(models.Model):
    """FIN-02: versioned withdrawal rules. Append-only: every change is a new version and each request
    keeps the version (and the fee it computed) it was submitted under."""

    class FeeMode(models.TextChoices):
        NONE = "NONE", _("No fee")
        FIXED = "FIXED", _("Fixed fee")
        PERCENT = "PERCENT", _("Percentage fee")

    class FeeBearer(models.TextChoices):
        DEDUCTED = "DEDUCTED", _("Deducted from the amount requested")
        ADDED = "ADDED", _("Added to the amount requested")

    id = models.BigAutoField(primary_key=True)
    version = models.PositiveIntegerField(_("version"), unique=True)
    min_amount = AmountField(_("minimum withdrawal (TZS)"), non_negative=True)
    max_amount = AmountField(_("maximum withdrawal (TZS)"), non_negative=True)
    daily_amount_limit = AmountField(_("daily amount limit (TZS)"), non_negative=True, null=True, blank=True)
    daily_request_limit = models.PositiveSmallIntegerField(_("daily requests"), null=True, blank=True)
    fee_mode = models.CharField(_("fee"), max_length=8, choices=FeeMode.choices, default=FeeMode.NONE)
    fee_fixed = AmountField(_("fixed fee (TZS)"), non_negative=True, default=0)
    fee_percent_bps = models.PositiveSmallIntegerField(_("percentage fee (basis points)"), default=0)
    fee_bearer = models.CharField(_("fee paid"), max_length=8, choices=FeeBearer.choices, default=FeeBearer.DEDUCTED)
    methods = models.JSONField(_("enabled methods"), default=list, blank=True)
    processing_hours = models.PositiveSmallIntegerField(_("usual processing time (hours)"), default=48)
    identity_required_from = AmountField(_("identity check from (TZS)"), non_negative=True, null=True, blank=True)
    requests_open = models.BooleanField(_("accept new requests"), default=True)
    payouts_open = models.BooleanField(_("allow new payout processing"), default=True)
    reason = models.CharField(_("reason for change"), max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    created_at = models.DateTimeField(_("created"), auto_now_add=True)

    class Meta:
        verbose_name = _("withdrawal settings version")
        ordering = ["-version"]
        permissions = [("manage_withdrawal_settings", "Can change withdrawal settings")]
        constraints = [
            models.CheckConstraint(condition=Q(min_amount__gt=0) & Q(max_amount__gte=F("min_amount")),
                                   name="wd_settings_amount_range"),
            models.CheckConstraint(condition=Q(daily_amount_limit__isnull=True) | Q(daily_amount_limit__gt=0),
                                   name="wd_settings_daily_amount_positive"),
            models.CheckConstraint(condition=Q(daily_request_limit__isnull=True) | Q(daily_request_limit__gt=0),
                                   name="wd_settings_daily_requests_positive"),
            models.CheckConstraint(condition=Q(fee_mode__in=["NONE", "FIXED", "PERCENT"])
                                   & Q(fee_bearer__in=["DEDUCTED", "ADDED"]), name="wd_settings_fee_choices"),
            models.CheckConstraint(condition=Q(fee_percent_bps__lte=5000), name="wd_settings_fee_percent_range"),
        ]

    def __str__(self):
        return f"Withdrawal settings v{self.version}"


class WithdrawalIdentity(BaseModel):
    """FIN-02: the identity check for withdrawals (separate from registration and membership access).
    The document number is stored encrypted; staff see the type and last four digits."""

    class IdType(models.TextChoices):
        NIDA = "NIDA", _("National ID (NIDA)")
        VOTER = "VOTER", _("Voter's ID")
        PASSPORT = "PASSPORT", _("Passport")
        DRIVING = "DRIVING", _("Driving licence")

    class Status(models.TextChoices):
        SUBMITTED = "SUBMITTED", _("Awaiting check")
        VERIFIED = "VERIFIED", _("Verified")
        REJECTED = "REJECTED", _("Not accepted")

    member = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="withdrawal_identity")
    legal_name = models.CharField(_("full legal name"), max_length=120)
    id_type = models.CharField(_("document type"), max_length=10, choices=IdType.choices)
    id_last4 = models.CharField(_("document number (last four)"), max_length=4)
    id_encrypted = models.TextField(_("document number (encrypted)"), editable=False)
    status = models.CharField(_("status"), max_length=10, choices=Status.choices, default=Status.SUBMITTED)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    reviewed_at = models.DateTimeField(_("checked"), null=True, blank=True)
    review_note = models.CharField(_("note"), max_length=300, blank=True)

    class Meta:
        verbose_name = _("withdrawal identity check")
        permissions = [("verify_withdrawal_identity", "Can verify withdrawal identity checks")]
        constraints = [
            models.CheckConstraint(condition=Q(status__in=["SUBMITTED", "VERIFIED", "REJECTED"]),
                                   name="wd_identity_status_valid"),
            models.CheckConstraint(condition=Q(reviewed_by__isnull=True) | ~Q(reviewed_by=F("member")),
                                   name="wd_identity_not_self_reviewed"),
            models.CheckConstraint(condition=Q(status="SUBMITTED") | (Q(reviewed_by__isnull=False)
                                                                     & Q(reviewed_at__isnull=False)),
                                   name="wd_identity_review_recorded"),
        ]


class WithdrawalRequest(BaseModel):
    """FIN-02: a REAL withdrawal. Submitting reserves the wallet deduction on the ledger
    (available -> reserved). Staff review and approve; a payout operator claims it exclusively,
    sends the money outside ClipCash and records the reference; a different person verifies the
    payment, which posts the payout (reserved -> cash, fee -> revenue). Rejection, cancellation and a
    verified failure release the reservation exactly once; an uncertain payout stays reserved in
    RECONCILIATION until the actual outcome is established. Database guards: migration 0003."""

    class Status(models.TextChoices):
        REQUESTED = "REQUESTED", _("Requested")
        UNDER_REVIEW = "UNDER_REVIEW", _("Under review")
        APPROVED = "APPROVED", _("Approved")
        PAYOUT_PENDING = "PAYOUT_PENDING", _("Payment processing")
        VERIFYING = "VERIFYING", _("Payment verification")
        PAID = "PAID", _("Completed")
        REJECTED = "REJECTED", _("Rejected")
        CANCELLED = "CANCELLED", _("Cancelled")
        FAILED = "FAILED", _("Failed")
        RECONCILIATION = "RECONCILIATION", _("Reconciliation required")

    class Method(models.TextChoices):
        AIRTEL = "AIRTEL", _("Airtel Money")
        MPESA = "MPESA", _("M-Pesa")
        MIXX = "MIXX", _("Mixx by Yas")
        HALOPESA = "HALOPESA", _("HaloPesa")
        BANK = "BANK", _("Bank transfer")

    reference = models.CharField(_("withdrawal reference"), max_length=16, unique=True,
                                 default=new_withdrawal_reference, editable=False)
    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="withdrawal_requests")
    wallet = models.ForeignKey("wallets.Wallet", on_delete=models.PROTECT, related_name="withdrawal_requests")
    beneficiary = models.ForeignKey(PayoutBeneficiary, on_delete=models.PROTECT, related_name="+")
    settings_version = models.ForeignKey(WithdrawalSettings, on_delete=models.PROTECT, related_name="+")
    method = models.CharField(_("method"), max_length=10, choices=Method.choices)
    # Immutable snapshot of the destination approved for this request.
    destination_kind = models.CharField(max_length=14)
    destination_provider = models.CharField(max_length=60)
    destination_holder = models.CharField(max_length=100)
    destination_last4 = models.CharField(max_length=4)
    destination_fingerprint = models.CharField(max_length=64)
    destination_encrypted = models.TextField(editable=False)
    amount = AmountField(_("amount requested (TZS)"), non_negative=True)
    fee = AmountField(_("fee (TZS)"), non_negative=True, default=0)
    fee_bearer = models.CharField(_("fee paid"), max_length=8, choices=WithdrawalSettings.FeeBearer.choices)
    wallet_deduction = AmountField(_("wallet deduction (TZS)"), non_negative=True)
    net_payout = AmountField(_("net payout (TZS)"), non_negative=True)
    currency = models.CharField(_("currency"), max_length=3, default=DEFAULT_CURRENCY)
    status = models.CharField(_("status"), max_length=16, choices=Status.choices, default=Status.REQUESTED)
    idempotency_key = models.CharField(_("idempotency key"), max_length=64, unique=True)
    identity_verified = models.BooleanField(_("identity verified when requested"), default=False)
    reason_code = models.CharField(_("reason"), max_length=40, blank=True)
    info_request = models.CharField(_("information requested"), max_length=300, blank=True)
    reserve_entry = models.OneToOneField("wallets.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="+")
    release_entry = models.OneToOneField("wallets.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="+")
    payout_entry = models.OneToOneField("wallets.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                        related_name="+")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    claimed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    claimed_at = models.DateTimeField(null=True, blank=True)
    payout_reference = models.CharField(_("external transaction reference"), max_length=40, blank=True)
    # "<method>:<key>" once recorded: one external transaction backs one payout (unique column).
    payout_reference_lock = models.CharField(max_length=60, null=True, blank=True, unique=True, editable=False)
    payout_evidence = models.CharField(_("payment evidence"), max_length=300, blank=True)
    recorded_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("withdrawal request")
        ordering = ["-created_at"]
        permissions = [
            ("approve_withdrawals", "Can approve real withdrawals"),
            ("process_payouts", "Can claim payouts and record external payments"),
            ("verify_payouts", "Can verify that an external payout was made"),
        ]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="wd_request_status_idx"),
            models.Index(fields=["member", "-created_at"], name="wd_request_member_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0) & Q(net_payout__gt=0) & Q(fee__gte=0),
                                   name="withdrawal_amount_positive"),
            models.CheckConstraint(
                condition=(Q(fee_bearer="DEDUCTED") & Q(wallet_deduction=F("amount"))
                           & Q(net_payout=F("amount") - F("fee")))
                | (Q(fee_bearer="ADDED") & Q(wallet_deduction=F("amount") + F("fee")) & Q(net_payout=F("amount"))),
                name="withdrawal_fee_arithmetic"),
            models.CheckConstraint(condition=Q(currency="TZS"), name="withdrawal_currency_tzs"),
            # Separation of duties.
            models.CheckConstraint(condition=Q(approved_by__isnull=True) | ~Q(approved_by=F("member")),
                                   name="withdrawal_no_self_approval"),
            models.CheckConstraint(condition=Q(claimed_by__isnull=True)
                                   | (~Q(claimed_by=F("approved_by")) & ~Q(claimed_by=F("member"))),
                                   name="withdrawal_approver_not_operator"),
            models.CheckConstraint(condition=Q(verified_by__isnull=True)
                                   | (~Q(verified_by=F("claimed_by")) & ~Q(verified_by=F("member"))),
                                   name="withdrawal_operator_not_verifier"),
            models.CheckConstraint(
                condition=~Q(status__in=["APPROVED", "PAYOUT_PENDING", "VERIFYING", "PAID"])
                | Q(approved_by__isnull=False), name="withdrawal_approval_required"),
            models.CheckConstraint(
                condition=~Q(status__in=["PAYOUT_PENDING", "VERIFYING", "PAID"]) | Q(claimed_by__isnull=False),
                name="withdrawal_claim_required"),
            models.CheckConstraint(
                condition=~Q(status__in=["VERIFYING", "PAID"]) | (~Q(payout_reference="")
                                                                   & Q(payout_reference_lock__isnull=False)),
                name="withdrawal_payment_recorded"),
            models.CheckConstraint(
                condition=~Q(status="PAID") | (Q(verified_by__isnull=False) & Q(payout_entry__isnull=False)),
                name="withdrawal_paid_verified"),
            models.CheckConstraint(
                condition=~Q(status__in=["REJECTED", "CANCELLED", "FAILED"]) | Q(reserve_entry__isnull=True)
                | Q(release_entry__isnull=False), name="withdrawal_release_recorded"),
            models.CheckConstraint(condition=Q(release_entry__isnull=True) | Q(payout_entry__isnull=True),
                                   name="withdrawal_released_or_paid"),
        ]

    def __str__(self):
        return self.reference

    @property
    def destination_label(self):
        return f"{self.destination_provider} •••• {self.destination_last4}"

    @property
    def is_open(self):
        return self.status in OPEN_WITHDRAWAL_STATES


OPEN_WITHDRAWAL_STATES = ("REQUESTED", "UNDER_REVIEW", "APPROVED", "PAYOUT_PENDING", "VERIFYING", "RECONCILIATION")


class WithdrawalEvent(models.Model):
    """Append-only history of a real withdrawal (who did what, when, and why)."""

    id = models.BigAutoField(primary_key=True)
    withdrawal = models.ForeignKey(WithdrawalRequest, on_delete=models.PROTECT, related_name="events")
    action = models.CharField(max_length=30)
    from_status = models.CharField(max_length=16, blank=True)
    to_status = models.CharField(max_length=16)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                              related_name="+")
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("withdrawal event")
        ordering = ["created_at", "id"]


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
