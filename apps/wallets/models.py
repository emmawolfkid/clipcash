"""
Double-entry ledger models.

Integrity is enforced by PostgreSQL (migration 0002), not only by Python:
balances are maintained by a trigger on line insert, entries must balance at
commit, posted rows are immutable, and the funding-separation rules in
apps.wallets.chart are checked for every entry. Only apps.wallets.services
should write to these tables.
"""

import secrets

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.core.fields import AmountField
from apps.core.models import BaseModel, UUIDModel
from apps.core.money import DEFAULT_CURRENCY, MAX_AMOUNT, SUPPORTED_CURRENCIES

from .chart import ACCOUNT_TYPES, Classification, FundingClass, Scope


class LedgerImmutableError(RuntimeError):
    """Raised on attempts to change or delete posted ledger records through the ORM."""


class ImmutableQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise LedgerImmutableError("Posted ledger records cannot be changed.")

    def delete(self):
        raise LedgerImmutableError("Posted ledger records cannot be deleted.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise LedgerImmutableError("Posted ledger records cannot be changed.")


def _type_constraints():
    """One CheckConstraint per derived attribute, generated from the chart."""
    by_attr = {}
    for attr in ("classification", "funding_class", "scope_type", "overdraft_protected"):
        source = "scope" if attr == "scope_type" else attr
        condition = Q()
        for code, account_type in ACCOUNT_TYPES.items():
            condition |= Q(account_type=code, **{attr: getattr(account_type, source)})
        by_attr[attr] = condition
    return [
        models.CheckConstraint(condition=by_attr["classification"], name="ledger_account_type_classification"),
        models.CheckConstraint(condition=by_attr["funding_class"], name="ledger_account_type_funding_class"),
        models.CheckConstraint(condition=by_attr["scope_type"], name="ledger_account_type_scope"),
        models.CheckConstraint(condition=by_attr["overdraft_protected"], name="ledger_account_type_protection"),
    ]


class LedgerAccount(BaseModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", _("Active")
        FROZEN = "FROZEN", _("Frozen")
        CLOSED = "CLOSED", _("Closed")

    code = models.CharField(_("code"), max_length=120, unique=True)
    name = models.CharField(_("name"), max_length=150)
    account_type = models.CharField(
        _("account type"), max_length=40, choices=[(c, t.name) for c, t in ACCOUNT_TYPES.items()]
    )
    classification = models.CharField(_("classification"), max_length=10, choices=Classification.choices)
    funding_class = models.CharField(_("funding class"), max_length=30, choices=FundingClass.choices)
    currency = models.CharField(_("currency"), max_length=3, default=DEFAULT_CURRENCY)
    status = models.CharField(_("status"), max_length=10, choices=Status.choices, default=Status.ACTIVE)
    scope_type = models.CharField(_("scope"), max_length=12, choices=Scope.choices)
    scope_id = models.CharField(_("scope id"), max_length=64, blank=True)
    overdraft_protected = models.BooleanField(_("overdraft protected"), default=False)
    # Sum of debits minus sum of credits. Maintained only by the database
    # trigger on journal line insert; direct updates are rejected.
    debit_balance = models.BigIntegerField(_("debit balance"), default=0, editable=False)

    class Meta:
        verbose_name = _("ledger account")
        verbose_name_plural = _("ledger accounts")
        ordering = ["code"]
        permissions = [("view_ledger", "Can view the financial ledger")]
        constraints = [
            models.UniqueConstraint(fields=["account_type", "scope_type", "scope_id"],
                                    name="ledger_account_type_scope_unique"),
            models.CheckConstraint(condition=Q(currency__in=SUPPORTED_CURRENCIES), name="ledger_account_currency"),
            models.CheckConstraint(
                condition=(Q(scope_type=Scope.PLATFORM, scope_id="") | (~Q(scope_type=Scope.PLATFORM) & ~Q(scope_id=""))),
                name="ledger_account_scope_id",
            ),
            # Overdraft protection: the normal-side balance may not go negative.
            models.CheckConstraint(
                condition=(
                    Q(overdraft_protected=False)
                    | Q(classification__in=Classification.DEBIT_NORMAL, debit_balance__gte=0)
                    | Q(classification__in=Classification.CREDIT_NORMAL, debit_balance__lte=0)
                ),
                name="ledger_account_no_overdraft",
            ),
            models.CheckConstraint(
                condition=Q(debit_balance__gte=-MAX_AMOUNT * 1000, debit_balance__lte=MAX_AMOUNT * 1000),
                name="ledger_account_balance_bounds",
            ),
            *_type_constraints(),
        ]

    def __str__(self):
        return f"{self.code} ({self.name})"

    @property
    def balance(self):
        """Balance on the account's normal side (positive = normal)."""
        if self.classification in Classification.DEBIT_NORMAL:
            return self.debit_balance
        return -self.debit_balance


class JournalEntry(UUIDModel):
    """A posted, balanced, immutable journal entry."""

    class Status(models.TextChoices):
        # Entries are only ever written already posted, in one transaction.
        POSTED = "POSTED", _("Posted")

    posting_type = models.CharField(_("posting type"), max_length=64)
    status = models.CharField(_("status"), max_length=10, choices=Status.choices, default=Status.POSTED)
    description = models.CharField(_("description"), max_length=255)
    currency = models.CharField(_("currency"), max_length=3, default=DEFAULT_CURRENCY)
    total_amount = models.BigIntegerField(_("total amount"))
    posted_at = models.DateTimeField(_("posted at"), auto_now_add=True)

    # Where the posting came from (business object or process).
    source_type = models.CharField(_("source type"), max_length=40)
    source_id = models.CharField(_("source id"), max_length=64, blank=True)
    external_reference = models.CharField(
        _("external reference"), max_length=100, blank=True,
        help_text=_("Provider or bank reference; unique per source type when set."),
    )
    related_entry = models.ForeignKey(
        "self", verbose_name=_("related entry"), null=True, blank=True,
        on_delete=models.PROTECT, related_name="dependent_entries",
    )
    reverses = models.OneToOneField(
        "self", verbose_name=_("reverses"), null=True, blank=True,
        on_delete=models.PROTECT, related_name="reversed_by",
    )

    idempotency_key = models.CharField(_("idempotency key"), max_length=128, unique=True)
    fingerprint = models.CharField(_("fingerprint"), max_length=64)
    actor_id = models.UUIDField(_("posted by"), null=True, blank=True)
    request_id = models.CharField(_("request id"), max_length=64, blank=True)

    objects = ImmutableQuerySet.as_manager()

    class Meta:
        verbose_name = _("journal entry")
        verbose_name_plural = _("journal entries")
        ordering = ["-posted_at"]
        indexes = [
            models.Index(fields=["-posted_at"], name="ledger_entry_posted_idx"),
            models.Index(fields=["posting_type", "-posted_at"], name="ledger_entry_type_idx"),
            models.Index(fields=["source_type", "source_id"], name="ledger_entry_source_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(status="POSTED"), name="ledger_entry_status"),
            models.CheckConstraint(condition=Q(currency__in=SUPPORTED_CURRENCIES), name="ledger_entry_currency"),
            models.CheckConstraint(condition=Q(total_amount__gt=0, total_amount__lte=MAX_AMOUNT * 100),
                                   name="ledger_entry_total_positive"),
            models.CheckConstraint(condition=~Q(idempotency_key=""), name="ledger_entry_idempotency_key"),
            models.CheckConstraint(condition=~Q(source_type=""), name="ledger_entry_source_type"),
            models.CheckConstraint(condition=Q(posting_type__regex=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$"),
                                   name="ledger_entry_posting_type_format"),
            models.UniqueConstraint(
                fields=["source_type", "external_reference"],
                condition=~Q(external_reference=""),
                name="ledger_entry_external_reference_unique",
            ),
            # A receipt can be verified once; an approved withdrawal paid once.
            models.UniqueConstraint(
                fields=["related_entry"],
                condition=Q(posting_type__in=["advertiser.funding_verified", "withdrawal.paid"]),
                name="ledger_entry_related_single_use",
            ),
            models.CheckConstraint(condition=~Q(reverses=models.F("id")), name="ledger_entry_not_self_reversal"),
        ]

    def __str__(self):
        return f"{self.posting_type} {self.total_amount} {self.currency} ({self.posted_at:%Y-%m-%d})"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise LedgerImmutableError("Posted journal entries cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise LedgerImmutableError("Posted journal entries cannot be deleted; post a reversal.")


class JournalLine(UUIDModel):
    class Side(models.TextChoices):
        DEBIT = "DEBIT", _("Debit")
        CREDIT = "CREDIT", _("Credit")

    entry = models.ForeignKey(JournalEntry, on_delete=models.PROTECT, related_name="lines")
    line_number = models.PositiveSmallIntegerField(_("line"))
    account = models.ForeignKey(LedgerAccount, on_delete=models.PROTECT, related_name="lines")
    side = models.CharField(_("side"), max_length=6, choices=Side.choices)
    amount = models.BigIntegerField(_("amount"))
    memo = models.CharField(_("memo"), max_length=200, blank=True)

    objects = ImmutableQuerySet.as_manager()

    class Meta:
        verbose_name = _("journal line")
        verbose_name_plural = _("journal lines")
        ordering = ["entry", "line_number"]
        indexes = [models.Index(fields=["account", "entry"], name="ledger_line_account_idx")]
        constraints = [
            models.UniqueConstraint(fields=["entry", "line_number"], name="ledger_line_number_unique"),
            models.UniqueConstraint(fields=["entry", "account"], name="ledger_line_account_unique"),
            models.CheckConstraint(condition=Q(amount__gt=0, amount__lte=MAX_AMOUNT), name="ledger_line_amount_positive"),
            models.CheckConstraint(condition=Q(side__in=["DEBIT", "CREDIT"]), name="ledger_line_side"),
        ]

    def __str__(self):
        return f"{self.side} {self.amount} {self.account.code}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise LedgerImmutableError("Posted journal lines cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise LedgerImmutableError("Posted journal lines cannot be deleted; post a reversal.")


# --- Member wallets (Phase 09) ------------------------------------------------------
#
# A wallet is an identity plus three member-scoped ledger accounts. It never
# stores money: balances are computed from JournalLine rows (apps.wallets.balances).
# In this phase every wallet balance is zero: the database refuses journal lines
# on wallet-linked accounts and any WalletTransaction (migration 0005). Lifting
# that needs a reviewed migration, not a setting or feature flag.


def new_wallet_number():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "CW-" + "".join(secrets.choice(alphabet) for _ in range(10))


class Wallet(BaseModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", _("Active")
        RESTRICTED = "RESTRICTED", _("Restricted")

    number = models.CharField(_("wallet number"), max_length=16, unique=True, default=new_wallet_number,
                              editable=False)
    member = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="wallet")
    currency = models.CharField(_("currency"), max_length=3, default=DEFAULT_CURRENCY)
    status = models.CharField(_("status"), max_length=12, choices=Status.choices, default=Status.ACTIVE)
    available_account = models.OneToOneField(LedgerAccount, on_delete=models.PROTECT,
                                             related_name="wallet_available")
    pending_account = models.OneToOneField(LedgerAccount, on_delete=models.PROTECT, related_name="wallet_pending")
    reserved_account = models.OneToOneField(LedgerAccount, on_delete=models.PROTECT, related_name="wallet_reserved")

    class Meta:
        verbose_name = _("member wallet")
        ordering = ["-created_at"]
        permissions = [
            ("view_wallets", "Can inspect member wallets"),
            ("manage_wallet_restrictions", "Can place and lift wallet restrictions"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(currency="TZS"), name="wallet_currency_supported"),
            models.CheckConstraint(condition=Q(status__in=["ACTIVE", "RESTRICTED"]), name="wallet_status_valid"),
        ]

    def __str__(self):
        return self.number


class WalletRestriction(BaseModel):
    class Kind(models.TextChoices):
        SECURITY_HOLD = "SECURITY_HOLD", _("Security hold")
        FRAUD_REVIEW = "FRAUD_REVIEW", _("Fraud review")
        COMPLIANCE = "COMPLIANCE", _("Compliance review")

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", _("Active")
        LIFT_REQUESTED = "LIFT_REQUESTED", _("Lift requested")
        LIFTED = "LIFTED", _("Lifted")

    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="restrictions")
    kind = models.CharField(_("kind"), max_length=16, choices=Kind.choices)
    reason = models.CharField(_("reason"), max_length=300)
    status = models.CharField(_("status"), max_length=16, choices=Status.choices, default=Status.ACTIVE)
    placed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    lift_requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                          on_delete=models.PROTECT, related_name="+")
    lift_reason = models.CharField(_("lift reason"), max_length=300, blank=True)
    lifted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="+")
    lifted_at = models.DateTimeField(_("lifted"), null=True, blank=True)

    class Meta:
        verbose_name = _("wallet restriction")
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=~Q(reason=""), name="wallet_restriction_reason"),
            # Two-person rule: whoever asks to lift a restriction cannot approve the lift.
            models.CheckConstraint(condition=Q(lifted_by__isnull=True)
                                   | ~Q(lifted_by=models.F("lift_requested_by")),
                                   name="wallet_restriction_four_eyes"),
            models.CheckConstraint(
                condition=(Q(status="ACTIVE") & Q(lift_requested_by__isnull=True) & Q(lifted_by__isnull=True))
                | (Q(status="LIFT_REQUESTED") & Q(lift_requested_by__isnull=False) & Q(lifted_by__isnull=True))
                | (Q(status="LIFTED") & Q(lift_requested_by__isnull=False) & Q(lifted_by__isnull=False)
                   & Q(lifted_at__isnull=False)),
                name="wallet_restriction_state_consistent"),
        ]


class WalletTransaction(UUIDModel):
    """Immutable projection of one journal entry onto one wallet bucket. FUTURE ONLY:
    the database refuses inserts in Phase 09 (migration 0005)."""

    class Category(models.TextChoices):
        REWARD_CREDIT = "REWARD_CREDIT", _("Advertiser-funded reward")
        WITHDRAWAL_RESERVATION = "WITHDRAWAL_RESERVATION", _("Withdrawal reserved")
        WITHDRAWAL_SETTLEMENT = "WITHDRAWAL_SETTLEMENT", _("Withdrawal paid")
        WITHDRAWAL_RELEASE = "WITHDRAWAL_RELEASE", _("Withdrawal released")
        CORRECTION = "CORRECTION", _("Authorized correction")
        REVERSAL = "REVERSAL", _("Reversal")

    class Bucket(models.TextChoices):
        AVAILABLE = "AVAILABLE", _("Available")
        PENDING = "PENDING", _("Pending")
        RESERVED = "RESERVED", _("Reserved")

    class Direction(models.TextChoices):
        IN = "IN", _("In")
        OUT = "OUT", _("Out")

    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="transactions")
    journal_entry = models.ForeignKey(JournalEntry, on_delete=models.PROTECT, related_name="wallet_transactions")
    reference = models.CharField(_("reference"), max_length=32, unique=True)
    idempotency_key = models.CharField(_("idempotency key"), max_length=128, unique=True)
    category = models.CharField(_("category"), max_length=24, choices=Category.choices)
    bucket = models.CharField(_("bucket"), max_length=10, choices=Bucket.choices)
    direction = models.CharField(_("direction"), max_length=3, choices=Direction.choices)
    amount = AmountField(_("amount (TZS)"), non_negative=True)
    created_at = models.DateTimeField(_("created"), auto_now_add=True)

    class Meta:
        verbose_name = _("wallet transaction")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["wallet", "-created_at"], name="wallet_txn_wallet_idx")]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="wallet_txn_amount_positive"),
            models.UniqueConstraint(fields=["wallet", "journal_entry", "bucket"],
                                    name="wallet_txn_entry_bucket_unique"),
        ]


class WalletDiscrepancy(BaseModel):
    """A reconciliation finding. Reported, never auto-corrected."""

    class Status(models.TextChoices):
        OPEN = "OPEN", _("Open")
        RESOLVED = "RESOLVED", _("Resolved")

    wallet = models.ForeignKey(Wallet, null=True, blank=True, on_delete=models.PROTECT,
                               related_name="discrepancies")
    account = models.ForeignKey(LedgerAccount, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    code = models.CharField(_("check"), max_length=40)
    detail = models.CharField(_("detail"), max_length=300, blank=True)
    status = models.CharField(_("status"), max_length=10, choices=Status.choices, default=Status.OPEN)
    last_seen_at = models.DateTimeField(_("last seen"), auto_now=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    resolved_at = models.DateTimeField(_("resolved"), null=True, blank=True)
    resolution_note = models.CharField(_("resolution note"), max_length=300, blank=True)

    class Meta:
        verbose_name = _("wallet discrepancy")
        verbose_name_plural = _("wallet discrepancies")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["wallet", "account", "code"], condition=Q(status="OPEN"),
                                    name="wallet_discrepancy_open_unique", nulls_distinct=False),
            models.CheckConstraint(condition=Q(status="OPEN") | (Q(resolved_by__isnull=False)
                                                                 & Q(resolved_at__isnull=False)),
                                   name="wallet_discrepancy_resolution_recorded"),
        ]
