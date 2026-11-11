"""
Payments and reconciliation (Phase 08), SANDBOX FIRST. Use apps.payments.services.

Two permanently separate funds domains:
* MEMBERSHIP_REVENUE: membership fees. Never funds advertiser rewards.
* ADVERTISER_RESTRICTED_FUNDS: an advertiser's own reconciled deposits, for its
  own campaigns only. Never created from demo budgets, membership money or
  reward records.

Exception (PAY-01): ManualPaymentInstruction and ManualMembershipPayment, at the end
of this module, record REAL membership fees paid manually to an Airtel Lipa business
number. They never touch PaymentIntent or the sandbox journal; see apps.payments.manual.

Every other row here is SANDBOX: the database refuses any other environment, and
sandbox postings live in their own journal (SandboxPosting), never in the real
ledger (apps.wallets). There is no switch that turns sandbox records into real
money; live payments need a separate, reviewed implementation.

Lifecycle of a payment intent (the provider callback alone never settles money):
  CREATED -> PENDING (provider request) -> PROVIDER_CONFIRMED (authenticated
  notice + authoritative status) -> RECONCILED (all checks matched) -> SETTLED
  (provider settlement confirmed). Exceptions go to ReconciliationException.
PostgreSQL guards: migration 0002.
"""

import secrets

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _

from apps.core.fields import AmountField
from apps.core.models import BaseModel
from apps.core.money import DEFAULT_CURRENCY
from apps.wallets.chart import ACCOUNT_TYPES
from apps.wallets.posting_types import POSTING_TYPES

SANDBOX = "SANDBOX"
OPEN_INTENT_STATES = ("CREATED", "PENDING", "PROVIDER_CONFIRMED")


def new_reference():
    """Internal payment reference: CC- plus 12 random base32 characters."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "CC-" + "".join(secrets.choice(alphabet) for _ in range(12))


class Environment(models.TextChoices):
    SANDBOX = "SANDBOX", _("Sandbox (simulated)")


class FundsDomain(models.TextChoices):
    MEMBERSHIP_REVENUE = "MEMBERSHIP_REVENUE", _("Membership revenue")
    ADVERTISER_RESTRICTED_FUNDS = "ADVERTISER_RESTRICTED_FUNDS", _("Advertiser restricted funds")


class PaymentIntent(BaseModel):
    class Purpose(models.TextChoices):
        MEMBERSHIP = "MEMBERSHIP", _("Membership fee")
        ADVERTISER_DEPOSIT = "ADVERTISER_DEPOSIT", _("Advertiser deposit")

    class Status(models.TextChoices):
        CREATED = "CREATED", _("Created")
        PENDING = "PENDING", _("Waiting for payment")
        PROVIDER_CONFIRMED = "PROVIDER_CONFIRMED", _("Confirmed by provider, checking")
        RECONCILED = "RECONCILED", _("Reconciled, awaiting settlement")
        SETTLED = "SETTLED", _("Settled")
        FAILED = "FAILED", _("Failed")
        EXPIRED = "EXPIRED", _("Expired")
        CANCELLED = "CANCELLED", _("Cancelled")
        REFUNDED = "REFUNDED", _("Refunded")
        DISPUTED = "DISPUTED", _("Disputed")

    reference = models.CharField(_("reference"), max_length=20, unique=True, default=new_reference, editable=False)
    environment = models.CharField(_("environment"), max_length=10, choices=Environment.choices,
                                   default=Environment.SANDBOX)
    purpose = models.CharField(_("purpose"), max_length=20, choices=Purpose.choices)
    funds_domain = models.CharField(_("funds domain"), max_length=30, choices=FundsDomain.choices)
    payer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payment_intents")
    organization = models.ForeignKey("advertising.Organization", null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="payment_intents")
    subscription = models.ForeignKey("memberships.Subscription", null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="payment_intents")
    amount = AmountField(_("amount (TZS)"), non_negative=True)
    currency = models.CharField(_("currency"), max_length=3, default=DEFAULT_CURRENCY)
    provider = models.CharField(_("provider"), max_length=20)
    provider_reference = models.CharField(_("provider reference"), max_length=64, blank=True)
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, default=Status.CREATED)
    status_reason = models.CharField(_("status reason"), max_length=100, blank=True)
    idempotency_key = models.CharField(_("idempotency key"), max_length=64)
    expires_at = models.DateTimeField(_("expires"))
    confirmed_at = models.DateTimeField(_("provider confirmed"), null=True, blank=True)
    reconciled_at = models.DateTimeField(_("reconciled"), null=True, blank=True)
    settled_at = models.DateTimeField(_("settled"), null=True, blank=True)
    closed_at = models.DateTimeField(_("closed"), null=True, blank=True)
    # Authoritative evidence from the provider (summary only; never raw payloads).
    provider_amount = AmountField(_("provider amount"), non_negative=True, null=True, blank=True)
    provider_currency = models.CharField(_("provider currency"), max_length=3, blank=True)

    class Meta:
        verbose_name = _("payment intent")
        ordering = ["-created_at"]
        permissions = [
            ("view_payments", "Can view payments and reconciliation"),
            ("operate_sandbox", "Can run the sandbox payment simulator"),
            ("review_reconciliation", "Can review reconciliation exceptions"),
            ("approve_refunds", "Can request and approve refunds and resolve disputes"),
        ]
        indexes = [
            models.Index(fields=["status", "expires_at"], name="payment_open_idx",
                         condition=Q(status__in=OPEN_INTENT_STATES)),
            models.Index(fields=["payer", "-created_at"], name="payment_payer_idx"),
            models.Index(fields=["organization", "-created_at"], name="payment_org_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(environment=SANDBOX), name="payment_sandbox_only"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="payment_amount_positive"),
            models.CheckConstraint(condition=Q(currency="TZS"), name="payment_currency_supported"),
            # Purpose fixes the funds domain and exactly one payer subject.
            models.CheckConstraint(
                condition=(Q(purpose="MEMBERSHIP", funds_domain="MEMBERSHIP_REVENUE", subscription__isnull=False,
                             organization__isnull=True))
                | (Q(purpose="ADVERTISER_DEPOSIT", funds_domain="ADVERTISER_RESTRICTED_FUNDS",
                     organization__isnull=False, subscription__isnull=True)),
                name="payment_purpose_consistent"),
            models.UniqueConstraint(fields=["provider", "provider_reference"], condition=~Q(provider_reference=""),
                                    name="payment_provider_reference_unique"),
            models.UniqueConstraint(fields=["payer", "idempotency_key"], name="payment_idempotency_unique"),
            models.UniqueConstraint(fields=["subscription"], condition=Q(status__in=OPEN_INTENT_STATES),
                                    name="payment_one_open_per_subscription"),
            models.CheckConstraint(condition=Q(status__in=["CREATED", "PENDING", "PROVIDER_CONFIRMED", "RECONCILED",
                                                           "SETTLED", "FAILED", "EXPIRED", "CANCELLED", "REFUNDED",
                                                           "DISPUTED"]), name="payment_status_valid"),
            models.CheckConstraint(condition=~Q(status__in=["PROVIDER_CONFIRMED", "RECONCILED", "SETTLED",
                                                            "REFUNDED", "DISPUTED"])
                                   | (Q(confirmed_at__isnull=False) & ~Q(provider_reference="")),
                                   name="payment_confirmed_has_evidence"),
            models.CheckConstraint(condition=~Q(status__in=["RECONCILED", "SETTLED", "REFUNDED", "DISPUTED"])
                                   | (Q(reconciled_at__isnull=False) & Q(provider_amount=F("amount"))
                                      & Q(provider_currency=F("currency"))),
                                   name="payment_reconciled_matches"),
            models.CheckConstraint(condition=~Q(status__in=["SETTLED", "REFUNDED", "DISPUTED"])
                                   | Q(settled_at__isnull=False), name="payment_settled_has_time"),
        ]

    def __str__(self):
        return self.reference

    @property
    def is_open(self):
        return self.status in OPEN_INTENT_STATES


class SandboxProviderTransaction(models.Model):
    """The SIMULATED external provider's own records (what a real provider's API would
    report). Changed only by the sandbox simulator; ClipCash reads it as untrusted-until-
    verified provider data."""

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        PAID = "PAID", _("Paid")
        FAILED = "FAILED", _("Failed")
        REFUNDED = "REFUNDED", _("Refunded")
        CHARGEBACK = "CHARGEBACK", _("Charged back")

    provider_reference = models.CharField(max_length=64, primary_key=True)
    intent_reference = models.CharField(max_length=20)
    amount = AmountField(non_negative=True)
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    settled = models.BooleanField(default=False)
    sequence = models.PositiveIntegerField(default=0)  # Bumped on each simulated change (event ids).
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("sandbox provider transaction (simulated)")


class ProviderEvent(models.Model):
    """Append-only log of provider notices and status checks (redacted summaries)."""

    class Kind(models.TextChoices):
        CALLBACK = "CALLBACK", _("Callback")
        POLL = "POLL", _("Status check")

    class Outcome(models.TextChoices):
        ACCEPTED = "ACCEPTED", _("Accepted")
        DUPLICATE = "DUPLICATE", _("Duplicate / replay")
        REFUSED = "REFUSED", _("Refused")
        EXCEPTION = "EXCEPTION", _("Sent to review")
        NO_CHANGE = "NO_CHANGE", _("No change")

    id = models.BigAutoField(primary_key=True)
    provider = models.CharField(max_length=20)
    event_id = models.CharField(max_length=100)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    authenticated = models.BooleanField(default=False)
    outcome = models.CharField(max_length=10, choices=Outcome.choices)
    reason = models.CharField(max_length=60, blank=True)
    provider_reference = models.CharField(max_length=64, blank=True)
    intent = models.ForeignKey(PaymentIntent, null=True, blank=True, on_delete=models.PROTECT,
                               related_name="provider_events")
    summary = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("provider event")
        ordering = ["-received_at"]
        constraints = [
            # Replay protection: each authenticated provider event is processed once.
            models.UniqueConstraint(fields=["provider", "event_id"], condition=Q(authenticated=True),
                                    name="provider_event_once"),
        ]


class ReconciliationException(BaseModel):
    class Reason(models.TextChoices):
        UNKNOWN_REFERENCE = "unknown_reference", _("Unknown provider reference")
        AMOUNT_MISMATCH = "amount_mismatch", _("Amount does not match")
        CURRENCY_MISMATCH = "currency_mismatch", _("Currency does not match")
        REFERENCE_MISMATCH = "reference_mismatch", _("Reference does not match")
        ENVIRONMENT_MISMATCH = "environment_mismatch", _("Environment does not match")
        INTENT_CLOSED = "intent_closed", _("Payment arrived for a closed intent")
        DUPLICATE_PAYMENT = "duplicate_payment", _("Duplicate payment")
        MEMBERSHIP_NOT_PENDING = "membership_not_pending", _("Membership no longer awaiting payment")
        FUNDS_SHORTFALL = "funds_shortfall", _("Restricted funds shortfall")
        FUNDING_INCONSISTENT = "funding_inconsistent", _("Funding totals inconsistent")

    class Status(models.TextChoices):
        OPEN = "OPEN", _("Open")
        RESOLVED = "RESOLVED", _("Resolved")
        DISMISSED = "DISMISSED", _("Dismissed")

    intent = models.ForeignKey(PaymentIntent, null=True, blank=True, on_delete=models.PROTECT,
                               related_name="exceptions")
    organization = models.ForeignKey("advertising.Organization", null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+")
    provider_reference = models.CharField(max_length=64, blank=True)
    reason = models.CharField(max_length=30, choices=Reason.choices)
    detail = models.CharField(max_length=300, blank=True)
    amount = AmountField(non_negative=True, null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.CharField(max_length=300, blank=True)

    class Meta:
        verbose_name = _("reconciliation exception")
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(status="OPEN") | (Q(resolved_by__isnull=False)
                                                                 & Q(resolved_at__isnull=False)),
                                   name="recon_exception_resolution_recorded"),
        ]


POSTING_CHOICES = [(code, t.description) for code, t in POSTING_TYPES.items()]
ACCOUNT_CHOICES = [(code, t.name) for code, t in ACCOUNT_TYPES.items()]


class SandboxPosting(models.Model):
    """
    Isolated SANDBOX journal. Each posting is one balanced pair (debit = credit =
    amount) of the real chart's account types under one of its controlled posting
    types, so the real funding-separation rules apply. Append-only; corrections are
    new postings that reference ``reverses``. Never copied into apps.wallets.
    """

    id = models.BigAutoField(primary_key=True)
    posting_type = models.CharField(max_length=40, choices=POSTING_CHOICES)
    debit_account = models.CharField(max_length=40, choices=ACCOUNT_CHOICES)
    debit_scope = models.CharField(max_length=64, blank=True)
    credit_account = models.CharField(max_length=40, choices=ACCOUNT_CHOICES)
    credit_scope = models.CharField(max_length=64, blank=True)
    amount = AmountField(non_negative=True)
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    funds_domain = models.CharField(max_length=30, choices=FundsDomain.choices)
    intent = models.ForeignKey(PaymentIntent, null=True, blank=True, on_delete=models.PROTECT,
                               related_name="sandbox_postings")
    idempotency_key = models.CharField(max_length=120, unique=True)
    memo = models.CharField(max_length=200, blank=True)
    posted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("sandbox journal posting (simulated)")
        ordering = ["posted_at", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="sandbox_posting_amount_positive"),
            models.CheckConstraint(condition=Q(currency="TZS"), name="sandbox_posting_currency"),
            models.CheckConstraint(condition=~Q(debit_account=F("credit_account"))
                                   | ~Q(debit_scope=F("credit_scope")), name="sandbox_posting_distinct_sides"),
        ]


class SandboxAdvertiserFunds(models.Model):
    """Per-advertiser SANDBOX restricted funds, derived only from settled deposits."""

    organization = models.OneToOneField("advertising.Organization", on_delete=models.PROTECT, primary_key=True,
                                        related_name="sandbox_funds")
    received = AmountField(non_negative=True, default=0)
    allocated = AmountField(non_negative=True, default=0)
    frozen = AmountField(non_negative=True, default=0)
    refunded = AmountField(non_negative=True, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("sandbox advertiser funds")
        constraints = [
            models.CheckConstraint(condition=Q(received__gte=0, allocated__gte=0, frozen__gte=0, refunded__gte=0),
                                   name="sandbox_funds_non_negative"),
            # Never more committed than received: available funds cannot go negative.
            models.CheckConstraint(condition=Q(received__gte=F("allocated") + F("frozen") + F("refunded")),
                                   name="sandbox_funds_within_received"),
        ]

    @property
    def available(self):
        return self.received - self.allocated - self.frozen - self.refunded


class SandboxCampaignAllocation(BaseModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", _("Allocated")
        RELEASED = "RELEASED", _("Released")

    organization = models.ForeignKey("advertising.Organization", on_delete=models.PROTECT,
                                     related_name="sandbox_allocations")
    campaign = models.ForeignKey("advertising.Campaign", on_delete=models.PROTECT, related_name="sandbox_allocations")
    amount = AmountField(non_negative=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    flagged = models.BooleanField(default=False)  # Affected by a refund/dispute shortfall.
    flag_reason = models.CharField(max_length=100, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    released_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")

    class Meta:
        verbose_name = _("sandbox campaign allocation")
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="sandbox_allocation_positive"),
            models.UniqueConstraint(fields=["campaign"], condition=Q(status="ACTIVE"),
                                    name="sandbox_allocation_one_active"),
            models.CheckConstraint(condition=(Q(status="ACTIVE") & Q(released_at__isnull=True))
                                   | (Q(status="RELEASED") & Q(released_at__isnull=False)),
                                   name="sandbox_allocation_release_recorded"),
        ]


class RefundRequest(BaseModel):
    class Status(models.TextChoices):
        REQUESTED = "REQUESTED", _("Awaiting approval")
        COMPLETED = "COMPLETED", _("Refunded")
        REJECTED = "REJECTED", _("Rejected")

    intent = models.ForeignKey(PaymentIntent, on_delete=models.PROTECT, related_name="refunds")
    amount = AmountField(non_negative=True)
    reason = models.CharField(max_length=300)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.REQUESTED)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=300, blank=True)
    provider_refund_reference = models.CharField(max_length=64, blank=True)

    class Meta:
        verbose_name = _("refund request")
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="refund_amount_positive"),
            models.UniqueConstraint(fields=["intent"], condition=Q(status__in=["REQUESTED", "COMPLETED"]),
                                    name="refund_one_per_intent"),
            models.CheckConstraint(condition=Q(decided_by__isnull=True) | ~Q(decided_by=F("requested_by")),
                                   name="refund_four_eyes"),
            models.CheckConstraint(condition=Q(status="REQUESTED") | (Q(decided_by__isnull=False)
                                                                      & Q(decided_at__isnull=False)),
                                   name="refund_decision_recorded"),
        ]


class PaymentDispute(BaseModel):
    class Status(models.TextChoices):
        OPEN = "OPEN", _("Open")
        WON = "WON", _("Resolved in our favour")
        LOST = "LOST", _("Charged back")

    intent = models.ForeignKey(PaymentIntent, on_delete=models.PROTECT, related_name="disputes")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    frozen_amount = AmountField(non_negative=True, default=0)
    shortfall_amount = AmountField(non_negative=True, default=0)
    opened_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="+")  # Null: opened by a provider chargeback notice.
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    resolved_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=300, blank=True)

    class Meta:
        verbose_name = _("payment dispute")
        constraints = [
            models.UniqueConstraint(fields=["intent"], condition=Q(status="OPEN"), name="dispute_one_open"),
            models.CheckConstraint(condition=Q(resolved_by__isnull=True) | Q(opened_by__isnull=True)
                                   | ~Q(resolved_by=F("opened_by")), name="dispute_four_eyes"),
            models.CheckConstraint(condition=Q(status="OPEN") | (Q(resolved_by__isnull=False)
                                                                 & Q(resolved_at__isnull=False)),
                                   name="dispute_resolution_recorded"),
        ]


class MembershipActivationPreview(BaseModel):
    """What a settled SANDBOX membership payment WOULD activate. Phase 03 subscriptions
    cannot mark sandbox entitlements, so nothing is activated or extended."""

    class Decision(models.TextChoices):
        WOULD_ACTIVATE = "WOULD_ACTIVATE", _("Would activate")
        WOULD_RENEW = "WOULD_RENEW", _("Would renew")
        DUPLICATE_PAYMENT = "DUPLICATE_PAYMENT", _("Duplicate payment")
        NOT_PENDING = "NOT_PENDING", _("Membership no longer awaiting payment")

    intent = models.OneToOneField(PaymentIntent, on_delete=models.PROTECT, related_name="membership_preview")
    subscription = models.ForeignKey("memberships.Subscription", on_delete=models.PROTECT, related_name="+")
    decision = models.CharField(max_length=20, choices=Decision.choices)
    would_start = models.DateTimeField(null=True, blank=True)
    would_end = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("membership activation preview (sandbox)")
        constraints = [
            # A subscription is (pretend-)activated by at most one payment: never extended twice.
            models.UniqueConstraint(fields=["subscription"], condition=Q(decision__in=["WOULD_ACTIVATE",
                                                                                      "WOULD_RENEW"]),
                                    name="membership_preview_once"),
        ]


# --- PAY-01: manual membership payments (REAL money, staff-verified) -----------------
#
# A member pays the exact membership price to ClipCash's Airtel Lipa business number
# from their own phone, then submits a claim (transaction reference, phone, amount).
# A claim is NEVER proof of payment: only an authorized staff member who finds the
# transaction in the official Airtel business records may approve it. Approval posts
# the fee to the real ledger (apps.wallets) and activates the membership exactly once.
# PostgreSQL guards: migration 0004 (transitions, immutability, single use).

MANUAL_METHOD_AIRTEL_LIPA = "AIRTEL_LIPA"
MANUAL_OPEN_OR_FINAL = ("PENDING_VERIFICATION", "APPROVED")


def new_manual_reference():
    """Internal claim reference: MP- plus 10 random base32 characters."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "MP-" + "".join(secrets.choice(alphabet) for _ in range(10))


class ManualPaymentMethod(models.TextChoices):
    AIRTEL_LIPA = MANUAL_METHOD_AIRTEL_LIPA, _("Airtel Money – Lipa")


class ManualPaymentInstruction(BaseModel):
    """Where members pay. Append-only history: publishing new details creates a new row
    and retires the previous one, so every claim keeps the details its member was shown.
    Entered by a Super Administrator; nothing here is hard-coded."""

    method = models.CharField(_("method"), max_length=20, choices=ManualPaymentMethod.choices,
                              default=MANUAL_METHOD_AIRTEL_LIPA)
    business_number = models.CharField(_("Lipa business number"), max_length=15)
    recipient_name = models.CharField(_("recipient name"), max_length=100)
    instructions = models.TextField(_("payment instructions"), max_length=1000, blank=True)
    is_active = models.BooleanField(_("active"), default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    retired_at = models.DateTimeField(_("retired"), null=True, blank=True)
    retired_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")

    class Meta:
        verbose_name = _("manual payment instruction")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["method"], condition=Q(is_active=True),
                                    name="manual_instruction_one_active"),
            models.CheckConstraint(condition=Q(business_number__regex=r"^[0-9]{4,15}$"),
                                   name="manual_instruction_number_digits"),
            models.CheckConstraint(condition=~Q(recipient_name=""), name="manual_instruction_recipient"),
            models.CheckConstraint(condition=Q(method__in=[MANUAL_METHOD_AIRTEL_LIPA]),
                                   name="manual_instruction_method"),
            # Active rows are never retired; retired rows say who retired them and when.
            models.CheckConstraint(condition=(Q(is_active=True) & Q(retired_at__isnull=True))
                                   | (Q(is_active=False) & Q(retired_at__isnull=False)
                                      & Q(retired_by__isnull=False)),
                                   name="manual_instruction_retirement_recorded"),
        ]

    def __str__(self):
        return f"{self.get_method_display()} {self.business_number} ({self.recipient_name})"


class ManualMembershipPayment(BaseModel):
    """A member's claim that they paid a membership fee manually. Untrusted until a
    staff member verifies it against the official Airtel business records."""

    class Status(models.TextChoices):
        PENDING_VERIFICATION = "PENDING_VERIFICATION", _("Pending verification")
        APPROVED = "APPROVED", _("Approved")
        REJECTED = "REJECTED", _("Rejected")
        CANCELLED = "CANCELLED", _("Cancelled")

    class RejectionReason(models.TextChoices):
        NOT_FOUND = "NOT_FOUND", _("No matching transaction in the Airtel business records")
        AMOUNT_MISMATCH = "AMOUNT_MISMATCH", _("The amount received does not match the plan price")
        REFERENCE_MISMATCH = "REFERENCE_MISMATCH", _("The transaction reference does not match")
        PAYER_MISMATCH = "PAYER_MISMATCH", _("The payment came from a different phone number")
        ALREADY_USED = "ALREADY_USED", _("The transaction was already used for another membership")
        OTHER = "OTHER", _("Other (see note)")

    reference = models.CharField(_("claim reference"), max_length=16, unique=True, default=new_manual_reference,
                                 editable=False)
    method = models.CharField(_("method"), max_length=20, choices=ManualPaymentMethod.choices,
                              default=MANUAL_METHOD_AIRTEL_LIPA)
    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                               related_name="manual_membership_payments")
    subscription = models.ForeignKey("memberships.Subscription", on_delete=models.PROTECT,
                                     related_name="manual_payments")
    instruction = models.ForeignKey(ManualPaymentInstruction, on_delete=models.PROTECT, related_name="claims")
    # Snapshots taken at submission (immutable; database-enforced).
    plan_name = models.CharField(_("plan"), max_length=100)
    amount = AmountField(_("amount (TZS)"), non_negative=True)
    currency = models.CharField(_("currency"), max_length=3, default=DEFAULT_CURRENCY)
    payer_phone = models.CharField(_("paying phone number"), max_length=16)
    transaction_reference = models.CharField(_("transaction reference"), max_length=40)
    # Canonical form (upper case, letters and digits only): the single-use key.
    reference_key = models.CharField(_("reference key"), max_length=40, editable=False)
    paid_on = models.DateField(_("payment date (stated by member)"), null=True, blank=True)

    status = models.CharField(_("status"), max_length=20, choices=Status.choices,
                              default=Status.PENDING_VERIFICATION)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    reviewed_at = models.DateTimeField(_("reviewed"), null=True, blank=True)
    # What the reviewer independently confirmed in the Airtel business records.
    verified_amount = AmountField(_("verified amount (TZS)"), non_negative=True, null=True, blank=True)
    verified_reference_key = models.CharField(_("verified reference"), max_length=40, blank=True)
    receipt_confirmed = models.BooleanField(_("receipt confirmed in Airtel records"), default=False)
    review_note = models.CharField(_("review note"), max_length=300, blank=True)
    rejection_reason = models.CharField(_("rejection reason"), max_length=20, choices=RejectionReason.choices,
                                        blank=True)
    ledger_entry_id = models.UUIDField(_("ledger entry"), null=True, blank=True)
    cancelled_at = models.DateTimeField(_("cancelled"), null=True, blank=True)

    class Meta:
        verbose_name = _("manual membership payment")
        ordering = ["-created_at"]
        permissions = [
            ("view_manual_payments", "Can view manual membership payments"),
            ("verify_manual_payments", "Can approve or reject manual membership payments"),
        ]
        indexes = [
            models.Index(fields=["status", "created_at"], name="manual_payment_queue_idx"),
            models.Index(fields=["member", "-created_at"], name="manual_payment_member_idx"),
            models.Index(fields=["reference_key"], name="manual_payment_refkey_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="manual_payment_amount_positive"),
            models.CheckConstraint(condition=Q(currency="TZS"), name="manual_payment_currency"),
            models.CheckConstraint(condition=Q(method__in=[MANUAL_METHOD_AIRTEL_LIPA]), name="manual_payment_method"),
            models.CheckConstraint(condition=Q(reference_key__regex=r"^[A-Z0-9]{6,40}$"),
                                   name="manual_payment_reference_key_format"),
            models.CheckConstraint(condition=Q(payer_phone__regex=r"^\+[1-9][0-9]{7,14}$"),
                                   name="manual_payment_phone_e164"),
            models.CheckConstraint(
                condition=Q(status__in=["PENDING_VERIFICATION", "APPROVED", "REJECTED", "CANCELLED"]),
                name="manual_payment_status_valid"),
            # One transaction reference backs at most one open or approved claim, ever.
            models.UniqueConstraint(fields=["reference_key"], condition=Q(status__in=MANUAL_OPEN_OR_FINAL),
                                    name="manual_payment_reference_single_use"),
            # One open claim, and at most one approved claim, per membership choice.
            models.UniqueConstraint(fields=["subscription"], condition=Q(status="PENDING_VERIFICATION"),
                                    name="manual_payment_one_open_per_subscription"),
            models.UniqueConstraint(fields=["subscription"], condition=Q(status="APPROVED"),
                                    name="manual_payment_one_approved_per_subscription"),
            # Nobody verifies their own payment.
            models.CheckConstraint(condition=Q(reviewed_by__isnull=True) | ~Q(reviewed_by=F("member")),
                                   name="manual_payment_not_self_reviewed"),
            # Approval records the independent verification and the ledger posting.
            models.CheckConstraint(
                condition=~Q(status="APPROVED")
                | (Q(reviewed_by__isnull=False, reviewed_at__isnull=False, receipt_confirmed=True,
                     ledger_entry_id__isnull=False)
                   & Q(verified_amount=F("amount")) & Q(verified_reference_key=F("reference_key"))
                   & Q(rejection_reason="")),
                name="manual_payment_approval_recorded"),
            models.CheckConstraint(
                condition=~Q(status="REJECTED")
                | (Q(reviewed_by__isnull=False, reviewed_at__isnull=False, ledger_entry_id__isnull=True)
                   & ~Q(rejection_reason="")),
                name="manual_payment_rejection_recorded"),
            models.CheckConstraint(
                condition=~Q(status="CANCELLED") | Q(cancelled_at__isnull=False, ledger_entry_id__isnull=True),
                name="manual_payment_cancellation_recorded"),
            models.CheckConstraint(
                condition=~Q(status="PENDING_VERIFICATION")
                | Q(reviewed_by__isnull=True, reviewed_at__isnull=True, ledger_entry_id__isnull=True),
                name="manual_payment_pending_unreviewed"),
        ]

    def __str__(self):
        return self.reference

    @property
    def is_pending(self):
        return self.status == self.Status.PENDING_VERIFICATION
