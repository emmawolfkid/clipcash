"""
FIN-02: real withdrawals paid manually by staff.

Flow (states in apps.withdrawals.models.WithdrawalRequest):

    REQUESTED ─review─▶ UNDER_REVIEW ─approve─▶ APPROVED ─claim─▶ PAYOUT_PENDING ─record─▶ VERIFYING ─verify─▶ PAID
        │                    │                     │                    │                      │
        └──reject/cancel─────┴──────reject/cancel──┘                    └──report problem──▶ RECONCILIATION
                                                                                                │
                              (verifier) PAYOUT_PENDING / VERIFYING / RECONCILIATION ─confirm failed─▶ FAILED
                              (verifier) RECONCILIATION (payment found) ─verify─▶ PAID

* Submitting reserves the wallet deduction on the ledger (withdrawal.reserved: available ->
  reserved) under the member row lock; the ledger's overdraft protection makes a second request
  for the same money fail. A repeated submission (same idempotency token) returns the first request.
* Rejection, cancellation (before a payout is claimed) and a verified failure release the
  reservation once (withdrawal.released, idempotency key per request). An uncertain payout never
  releases: it stays reserved in RECONCILIATION until a verifier establishes what happened.
* Separation of duties (also database checks): the member never decides; the approver never
  processes; the operator who records the payment never verifies it.
* The payout is claimed exclusively (row lock + expected state); a claim never expires into a
  second payment: only a person can move it on, and a failure must be confirmed by another person.
* The external reference is unique per channel; it is never proof by itself: a verifier must
  confirm the payment, and only then withdrawal.paid_manual posts (reserved -> cash, fee -> revenue).
* New requests and processing need the ``withdrawals`` feature and the operational switches;
  settling existing obligations needs only ``withdrawal_settlement``.
"""

import hashlib
import logging
import math
import re
from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.utils.translation import gettext_noop as N_

from apps.audit.services import DENIED, SUCCESS, record_business_event, record_security_event
from apps.core import features
from apps.wallets import services as ledger
from apps.wallets.exceptions import InsufficientFundsError
from apps.wallets.models import JournalEntry, Wallet, WalletTransaction

from . import protect
from .models import (
    OPEN_WITHDRAWAL_STATES,
    PayoutBeneficiary,
    WithdrawalEvent,
    WithdrawalIdentity,
    WithdrawalRequest,
    WithdrawalSettings,
)

log = logging.getLogger("clipcash.withdrawals")
W = WithdrawalRequest.Status
S = WithdrawalSettings
METHODS = WithdrawalRequest.Method
SOURCE_RESERVE = "withdrawal"
SOURCE_PAYOUT = "withdrawal_payout"
REFERENCE_RE = re.compile(r"^[A-Z0-9][A-Z0-9 .\-/]*[A-Z0-9]$")
REJECTION_REASONS = ("destination_mismatch", "identity_not_verified", "suspected_fraud", "limit_exceeded",
                     "member_request", "other")
REASON_TEXT = {
    "destination_mismatch": N_("The payout destination could not be confirmed."),
    "identity_not_verified": N_("Your identity check is not complete."),
    "suspected_fraud": N_("The request did not pass our security review."),
    "limit_exceeded": N_("The request is above an allowed limit."),
    "member_request": N_("Cancelled at your request."),
    "other": N_("See the message from ClipCash support."),
    "payout_failed": N_("The transfer did not reach your account."),
}
# Tanzanian banks offered for bank transfers (code -> name). A directory, not a financial rule.
TANZANIA_BANKS = {
    "CRDB": "CRDB Bank", "NMB": "NMB Bank", "NBC": "NBC Bank", "STANBIC": "Stanbic Bank Tanzania",
    "ABSA": "Absa Bank Tanzania", "EQUITY": "Equity Bank Tanzania", "EXIM": "Exim Bank Tanzania",
    "DTB": "Diamond Trust Bank", "KCB": "KCB Bank Tanzania", "AZANIA": "Azania Bank", "TCB": "Tanzania Commercial Bank",
    "AKIBA": "Akiba Commercial Bank", "AMANA": "Amana Bank", "BOA": "Bank of Africa Tanzania",
    "ECOBANK": "Ecobank Tanzania", "IANDM": "I&M Bank Tanzania", "MKOMBOZI": "Mkombozi Commercial Bank",
    "PBZ": "People's Bank of Zanzibar",
}
NETWORK_METHOD = {"AIRTEL": METHODS.AIRTEL, "MPESA": METHODS.MPESA, "MIXX": METHODS.MIXX, "HALOPESA": METHODS.HALOPESA}


class WithdrawalFlowError(Exception):
    def __init__(self, message, *, code="invalid", reasons=None):
        super().__init__(message)
        self.code = code
        self.reasons = reasons or []
        self.messages = [str(message)]


class NotAuthorized(WithdrawalFlowError):
    pass


class StaleState(WithdrawalFlowError):
    pass


# --- Activation and settings ---------------------------------------------------------------------


def requests_active():
    return features.is_enabled("withdrawals")


def settlement_active():
    return features.is_enabled("withdrawal_settlement")


def _default_values():
    return {"min_amount": settings.WITHDRAWALS_MIN_AMOUNT, "max_amount": settings.WITHDRAWALS_MAX_AMOUNT,
            "daily_amount_limit": settings.WITHDRAWALS_DAILY_LIMIT,
            "daily_request_limit": settings.WITHDRAWALS_MAX_REQUESTS_PER_DAY,
            "methods": list(METHODS.values), "identity_required_from": 0}


def current_settings():
    """The latest settings version (version 1 is created from the deployment defaults)."""
    row = WithdrawalSettings.objects.order_by("-version").first()
    if row is None:
        try:
            with transaction.atomic():
                row = WithdrawalSettings.objects.create(version=1, reason="Initial defaults", **_default_values())
        except IntegrityError:
            row = WithdrawalSettings.objects.order_by("-version").first()
    return row


SETTING_FIELDS = ("min_amount", "max_amount", "daily_amount_limit", "daily_request_limit", "fee_mode", "fee_fixed",
                  "fee_percent_bps", "fee_bearer", "methods", "processing_hours", "identity_required_from",
                  "requests_open", "payouts_open")


def _staff_with(user, perm):
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.is_active
                and user.is_staff and user.has_perm(perm))


def can_manage_settings(user):
    return _staff_with(user, "withdrawals.manage_withdrawal_settings")


def can_approve(user):
    return _staff_with(user, "withdrawals.approve_withdrawals")


def can_process(user):
    return _staff_with(user, "withdrawals.process_payouts")


def can_verify_payouts(user):
    return _staff_with(user, "withdrawals.verify_payouts")


def can_verify_identity(user):
    return _staff_with(user, "withdrawals.verify_withdrawal_identity")


def can_view(user):
    return (_staff_with(user, "withdrawals.view_withdrawals") or can_approve(user) or can_process(user)
            or can_verify_payouts(user) or can_manage_settings(user) or can_verify_identity(user))


def _require(check, actor, action, target=None):
    if not check(actor):
        record_security_event("withdrawals.action.denied", outcome=DENIED,
                              actor=actor if getattr(actor, "is_authenticated", False) else None, target=target,
                              metadata={"action": action, "reason": "permission_missing"})
        raise NotAuthorized(_("You are not allowed to do this."), code="permission_missing")


def _reason_text(text, minimum=5):
    text = (text or "").strip()
    if len(text) < minimum:
        raise ValidationError({"reason": _("Please give a reason (at least 5 characters).")})
    return text[:300]


def update_settings(actor, *, expected_version, reason, **values):
    """Create the next settings version. Requests already submitted keep their own version."""
    _require(can_manage_settings, actor, "settings.update")
    reason = _reason_text(reason)
    cleaned = _clean_settings(values)
    with transaction.atomic():
        current = WithdrawalSettings.objects.select_for_update().order_by("-version").first() or current_settings()
        if str(current.version) != str(expected_version):
            raise ValidationError(_("These settings were changed by someone else. Reload and try again."))
        changes = [f"{name}: {getattr(current, name)} -> {cleaned[name]}" for name in SETTING_FIELDS
                   if getattr(current, name) != cleaned[name]]
        if not changes:
            return current
        row = WithdrawalSettings.objects.create(version=current.version + 1, created_by=actor, reason=reason,
                                                **cleaned)
        record_business_event("withdrawals.settings.updated", actor=actor, target=row, metadata={
            "version": row.version, "changes": "; ".join(changes)[:200], "reason": reason[:100]})
    return row


def _int(raw, field, *, optional=False, minimum=0, maximum=1_000_000_000):
    text = str(raw if raw is not None else "").strip().replace(",", "")
    if text == "" and optional:
        return None
    if not text.isdigit() or not minimum <= int(text) <= maximum:
        raise ValidationError({field: _("Enter a whole number between %(min)s and %(max)s.")
                               % {"min": f"{minimum:,}", "max": f"{maximum:,}"}})
    return int(text)


def _clean_settings(values):
    cleaned = {
        "min_amount": _int(values.get("min_amount"), "min_amount", minimum=1),
        "max_amount": _int(values.get("max_amount"), "max_amount", minimum=1),
        "daily_amount_limit": _int(values.get("daily_amount_limit"), "daily_amount_limit", optional=True, minimum=1),
        "daily_request_limit": _int(values.get("daily_request_limit"), "daily_request_limit", optional=True,
                                    minimum=1, maximum=100),
        "fee_mode": values.get("fee_mode") or S.FeeMode.NONE,
        "fee_fixed": _int(values.get("fee_fixed") or 0, "fee_fixed", maximum=1_000_000),
        "fee_percent_bps": _int(values.get("fee_percent_bps") or 0, "fee_percent_bps", maximum=5000),
        "fee_bearer": values.get("fee_bearer") or S.FeeBearer.DEDUCTED,
        "methods": sorted({m for m in (values.get("methods") or []) if m in METHODS.values}),
        "processing_hours": _int(values.get("processing_hours"), "processing_hours", minimum=1, maximum=720),
        "identity_required_from": _int(values.get("identity_required_from"), "identity_required_from",
                                       optional=True, maximum=1_000_000_000),
        "requests_open": bool(values.get("requests_open")),
        "payouts_open": bool(values.get("payouts_open")),
    }
    if cleaned["fee_mode"] not in S.FeeMode.values or cleaned["fee_bearer"] not in S.FeeBearer.values:
        raise ValidationError({"fee_mode": _("Choose how the fee works.")})
    if cleaned["max_amount"] < cleaned["min_amount"]:
        raise ValidationError({"max_amount": _("The maximum must be at least the minimum.")})
    if cleaned["fee_mode"] != S.FeeMode.FIXED:
        cleaned["fee_fixed"] = 0
    if cleaned["fee_mode"] != S.FeeMode.PERCENT:
        cleaned["fee_percent_bps"] = 0
    return cleaned


# --- Fees ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Quote:
    amount: int
    fee: int
    fee_bearer: str
    wallet_deduction: int
    net_payout: int


def quote(amount, cfg):
    """Fee, wallet deduction and net payout for ``amount`` (whole TZS). Percentage fees are rounded
    UP to the next whole shilling (ceil(amount x basis points / 10,000)); fixed fees are exact."""
    if cfg.fee_mode == S.FeeMode.FIXED:
        fee = cfg.fee_fixed
    elif cfg.fee_mode == S.FeeMode.PERCENT:
        fee = math.ceil(amount * cfg.fee_percent_bps / 10_000)
    else:
        fee = 0
    if cfg.fee_bearer == S.FeeBearer.ADDED:
        return Quote(amount, fee, cfg.fee_bearer, amount + fee, amount)
    return Quote(amount, fee, cfg.fee_bearer, amount, amount - fee)


# --- Identity (separate from registration) ------------------------------------------------------


def identity_of(member):
    return WithdrawalIdentity.objects.filter(member=member).first()


def identity_required(amount, cfg):
    return cfg.identity_required_from is not None and amount is not None and amount >= cfg.identity_required_from


def submit_identity(member, *, legal_name, id_type, id_number):
    legal_name = " ".join((legal_name or "").split())[:120]
    number = re.sub(r"[\s-]", "", str(id_number or "")).upper()
    errors = {}
    if len(legal_name) < 5:
        errors["legal_name"] = _("Enter your full name as it appears on the document.")
    if id_type not in WithdrawalIdentity.IdType.values:
        errors["id_type"] = _("Choose the document type.")
    if not (6 <= len(number) <= 25 and number.isalnum()):
        errors["id_number"] = _("Enter the document number (6 to 25 letters or digits).")
    if errors:
        raise ValidationError(errors)
    encrypted = protect.encrypt(number)
    with transaction.atomic():
        identity = WithdrawalIdentity.objects.select_for_update().filter(member=member).first()
        if identity is not None and identity.status == WithdrawalIdentity.Status.VERIFIED:
            raise WithdrawalFlowError(_("Your identity is already verified. Contact support to change it."),
                                      code="already_verified")
        if identity is None:
            identity = WithdrawalIdentity(member=member)
        identity.legal_name, identity.id_type, identity.id_last4 = legal_name, id_type, number[-4:]
        identity.id_encrypted, identity.status = encrypted, WithdrawalIdentity.Status.SUBMITTED
        identity.reviewed_by, identity.reviewed_at, identity.review_note = None, None, ""
        identity.save()
        record_business_event("withdrawals.identity.submitted", actor=member, target=identity,
                              metadata={"id_type": id_type})
        _notify_staff("withdrawals.verify_withdrawal_identity", "identity_review", identity.pk,
                      N_("An identity check awaits review"),
                      N_("A member submitted an identity check for withdrawals."), "administration:withdrawal_identities")
    return identity


def review_identity(actor, identity, *, approve, note=""):
    _require(can_verify_identity, actor, "identity.review", identity)
    if identity.member_id == actor.pk:
        raise NotAuthorized(_("You cannot check your own identity."), code="self_review")
    note = (note or "").strip()[:300]
    if not approve and len(note) < 5:
        raise ValidationError({"note": _("Please give a reason (at least 5 characters).")})
    with transaction.atomic():
        identity = WithdrawalIdentity.objects.select_for_update().get(pk=identity.pk)
        if identity.status != WithdrawalIdentity.Status.SUBMITTED:
            raise StaleState(_("This identity check was already decided."), code="stale")
        identity.status = WithdrawalIdentity.Status.VERIFIED if approve else WithdrawalIdentity.Status.REJECTED
        identity.reviewed_by, identity.reviewed_at, identity.review_note = actor, timezone.now(), note
        identity.save()
        record_business_event("withdrawals.identity.reviewed", actor=actor, target=identity,
                              metadata={"decision": "verified" if approve else "rejected"})
        from apps.notifications import inbox

        inbox.notify(identity.member, f"withdrawals.identity_{'verified' if approve else 'rejected'}",
                     category=inbox.Category.WITHDRAWAL,
                     title=N_("Identity check complete") if approve else N_("Identity check not accepted"),
                     message=N_("You can now request withdrawals that need an identity check.") if approve
                     else N_("Please check your details and submit them again."), params={},
                     link=inbox.link_to("withdrawals:overview"),
                     dedupe_key=f"withdrawals.identity:{identity.pk}:{identity.reviewed_at.isoformat()}")
    return identity


# --- Requests -----------------------------------------------------------------------------------


def method_for(beneficiary):
    if beneficiary.kind == PayoutBeneficiary.Kind.BANK:
        return METHODS.BANK
    return NETWORK_METHOD.get(beneficiary.network, "")


def used_today(member, now=None):
    """(requests, TZS requested) today in the site time zone; cancelled/rejected requests still count
    toward the number of requests, released money does not count toward the amount."""
    from apps.earnings.services import day_bounds, local_day

    start, end = day_bounds(local_day(now or timezone.now()))
    today = WithdrawalRequest.objects.filter(member=member, created_at__gte=start, created_at__lt=end)
    total = today.exclude(status__in=[W.REJECTED, W.CANCELLED, W.FAILED]).aggregate(
        t=Coalesce(Sum("amount"), 0))["t"]
    return today.count(), total


def _idempotency_key(member, token):
    token = str(token or "").strip()
    if not 16 <= len(token) <= 80:
        raise ValidationError(_("This form has expired. Reload the page and try again."))
    return hashlib.sha256(f"{member.pk}:{token}".encode()).hexdigest()


def _event(withdrawal, action, from_status, *, actor=None, note=""):
    WithdrawalEvent.objects.create(withdrawal=withdrawal, action=action, from_status=from_status,
                                   to_status=withdrawal.status, actor=actor, note=(note or "")[:300])


def _wallet_txn(wallet, entry, category, bucket, direction, amount):
    return WalletTransaction.objects.create(
        wallet=wallet, journal_entry=entry, reference=f"WT-{str(entry.pk)[:8].upper()}-{bucket[:1]}",
        idempotency_key=f"{entry.pk}:{bucket}", category=category, bucket=bucket, direction=direction, amount=amount)


def submit(member, *, beneficiary_id, amount, token):
    """Create the request and reserve the wallet deduction atomically. Idempotent per token."""
    from .services import ELIGIBILITY_MESSAGES, withdrawal_eligibility

    key = _idempotency_key(member, token)
    existing = WithdrawalRequest.objects.filter(idempotency_key=key, member=member).first()
    if existing is not None:
        return existing
    if not requests_active():
        raise WithdrawalFlowError(ELIGIBILITY_MESSAGES["withdrawals_locked"], code="withdrawals_locked",
                                  reasons=["withdrawals_locked"])
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=member.pk)  # One request at a time per member.
        existing = WithdrawalRequest.objects.filter(idempotency_key=key, member=member).first()
        if existing is not None:
            return existing
        beneficiary = PayoutBeneficiary.objects.filter(pk=beneficiary_id, member=member,
                                                       status=PayoutBeneficiary.Status.ACTIVE).first()
        decision = withdrawal_eligibility(member, amount=amount, beneficiary=beneficiary, require_destination=True)
        if not decision:
            raise WithdrawalFlowError(" ".join(str(ELIGIBILITY_MESSAGES.get(c, c)) for c in decision.reasons),
                                      code="not_eligible", reasons=decision.reasons)
        cfg, figures = decision.settings, decision.quote
        wallet = Wallet.objects.select_for_update().get(member=member)
        identity = identity_of(member)
        request = WithdrawalRequest.objects.create(
            member=member, wallet=wallet, beneficiary=beneficiary, settings_version=cfg,
            method=method_for(beneficiary), destination_kind=beneficiary.kind,
            destination_provider=beneficiary.destination_label.rsplit(" ", 2)[0],
            destination_holder=beneficiary.holder_name, destination_last4=beneficiary.identifier_last4,
            destination_fingerprint=beneficiary.identifier_fingerprint,
            destination_encrypted=beneficiary.identifier_encrypted, amount=figures.amount, fee=figures.fee,
            fee_bearer=figures.fee_bearer, wallet_deduction=figures.wallet_deduction, net_payout=figures.net_payout,
            idempotency_key=key,
            identity_verified=bool(identity and identity.status == WithdrawalIdentity.Status.VERIFIED))
        try:
            entry = ledger.post_entry(
                "withdrawal.reserved",
                lines=[ledger.debit(wallet.available_account, figures.wallet_deduction, "Withdrawal requested"),
                       ledger.credit(wallet.reserved_account, figures.wallet_deduction, "Reserved for withdrawal")],
                idempotency_key=f"withdrawal-reserve:{request.pk}", source_type=SOURCE_RESERVE,
                source_id=str(request.pk), description=f"Withdrawal {request.reference} reserved", actor=None)
        except InsufficientFundsError:
            raise WithdrawalFlowError(ELIGIBILITY_MESSAGES["insufficient_balance"], code="insufficient_balance",
                                      reasons=["insufficient_balance"]) from None
        _wallet_txn(wallet, entry, WalletTransaction.Category.WITHDRAWAL_RESERVATION,
                    WalletTransaction.Bucket.AVAILABLE, WalletTransaction.Direction.OUT, figures.wallet_deduction)
        _wallet_txn(wallet, entry, WalletTransaction.Category.WITHDRAWAL_RESERVATION,
                    WalletTransaction.Bucket.RESERVED, WalletTransaction.Direction.IN, figures.wallet_deduction)
        request.reserve_entry = entry
        request.save(update_fields=["reserve_entry", "updated_at"])
        _event(request, "requested", "", actor=member)
        record_business_event("withdrawals.request.submitted", actor=member, target=request, metadata={
            "reference": request.reference, "amount": request.amount, "fee": request.fee,
            "method": request.method, "settings_version": cfg.version})
        _notify_member(request, "requested")
        _notify_staff("withdrawals.approve_withdrawals", "review_needed", request.pk,
                      N_("A withdrawal awaits review"), N_("Withdrawal {reference} needs review."),
                      "administration:withdrawal_request", reference=request.reference)
    return request


def _locked(request):
    return WithdrawalRequest.objects.select_for_update().select_related("wallet", "member").get(pk=request.pk)


def _expect(request, expected, allowed):
    if expected and request.status != expected:
        raise StaleState(_("This withdrawal changed since you opened it. Reload the page."), code="stale")
    if request.status not in allowed:
        raise StaleState(_("This action is not possible in the current state."), code="invalid_state")


def _wallet_restricted(request):
    return Wallet.objects.get(pk=request.wallet_id).status == Wallet.Status.RESTRICTED


def _release(request, action, actor, *, reason_code="", note=""):
    """Release the reservation exactly once (inside the caller's transaction, request locked)."""
    wallet = request.wallet
    entry = ledger.post_entry(
        "withdrawal.released",
        lines=[ledger.debit(wallet.reserved_account, request.wallet_deduction, "Reservation released"),
               ledger.credit(wallet.available_account, request.wallet_deduction, "Returned to available balance")],
        idempotency_key=f"withdrawal-release:{request.pk}", source_type=SOURCE_RESERVE, source_id=str(request.pk),
        description=f"Withdrawal {request.reference} released ({action})", actor=None)
    _wallet_txn(wallet, entry, WalletTransaction.Category.WITHDRAWAL_RELEASE, WalletTransaction.Bucket.RESERVED,
                WalletTransaction.Direction.OUT, request.wallet_deduction)
    _wallet_txn(wallet, entry, WalletTransaction.Category.WITHDRAWAL_RELEASE, WalletTransaction.Bucket.AVAILABLE,
                WalletTransaction.Direction.IN, request.wallet_deduction)
    request.release_entry = entry
    return entry


def _transition(request, status, action, actor, *, note="", audit_action=None, **fields):
    previous = request.status
    request.status = status
    for name, value in fields.items():
        setattr(request, name, value)
    request.save()
    _event(request, action, previous, actor=actor, note=note)
    record_business_event(audit_action or f"withdrawals.request.{action}", actor=actor, target=request, metadata={
        "reference": request.reference, "from_status": previous, "to_status": status})
    return request


def start_review(actor, request, *, expected_status):
    _require(can_approve, actor, "request.review", request)
    with transaction.atomic():
        request = _locked(request)
        _expect(request, expected_status, (W.REQUESTED,))
        if request.member_id == actor.pk:
            raise NotAuthorized(_("You cannot review your own withdrawal."), code="self_review")
        return _transition(request, W.UNDER_REVIEW, "review", actor, reviewed_by=actor)


def request_information(actor, request, *, note):
    _require(can_approve, actor, "request.info", request)
    note = _reason_text(note)
    with transaction.atomic():
        request = _locked(request)
        _expect(request, "", (W.REQUESTED, W.UNDER_REVIEW))
        status = W.UNDER_REVIEW
        _transition(request, status, "info_requested", actor, note=note, info_request=note, reviewed_by=actor)
        _notify_member(request, "info_requested")
    return request


def approve(actor, request, *, expected_status, note=""):
    _require(can_approve, actor, "request.approve", request)
    with transaction.atomic():
        request = _locked(request)
        _expect(request, expected_status, (W.REQUESTED, W.UNDER_REVIEW))
        if request.member_id == actor.pk:
            raise NotAuthorized(_("You cannot approve your own withdrawal."), code="self_review")
        if _wallet_restricted(request):
            raise WithdrawalFlowError(_("The member's wallet is restricted. Reject the request or wait for the "
                                        "review to finish."), code="wallet_restricted")
        identity = identity_of(request.member)
        if identity_required(request.amount, request.settings_version) and not (
                identity and identity.status == WithdrawalIdentity.Status.VERIFIED):
            raise WithdrawalFlowError(_("The member's identity check is not verified yet."),
                                      code="identity_not_verified")
        _transition(request, W.APPROVED, "approved", actor, note=note, approved_by=actor,
                    approved_at=timezone.now(), reviewed_by=request.reviewed_by or actor)
        _notify_member(request, "approved")
        _notify_staff("withdrawals.process_payouts", "payout_ready", request.pk, N_("A payout is ready to send"),
                      N_("Withdrawal {reference} is approved and waiting for a payout operator."),
                      "administration:withdrawal_request", reference=request.reference, exclude=(actor.pk,))
    return request


def reject(actor, request, *, expected_status, reason_code, note=""):
    _require(can_approve, actor, "request.reject", request)
    if reason_code not in REJECTION_REASONS:
        raise ValidationError({"reason_code": _("Choose a reason.")})
    note = (note or "").strip()[:300]
    with transaction.atomic():
        request = _locked(request)
        _expect(request, expected_status, (W.REQUESTED, W.UNDER_REVIEW, W.APPROVED))
        if request.member_id == actor.pk:
            raise NotAuthorized(_("You cannot decide your own withdrawal."), code="self_review")
        _release(request, "rejected", actor)
        _transition(request, W.REJECTED, "rejected", actor, note=note, reason_code=reason_code, decided_by=actor,
                    finished_at=timezone.now(), release_entry=request.release_entry)
        _notify_member(request, "rejected")
    return request


def cancel(member, request):
    """The member cancels before a payout operator has claimed the payout."""
    with transaction.atomic():
        request = WithdrawalRequest.objects.select_for_update().select_related("wallet").filter(
            pk=request.pk, member=member).first()
        if request is None:
            raise WithdrawalFlowError(_("Withdrawal not found."), code="not_found")
        if request.status == W.CANCELLED:
            return request
        _expect(request, "", (W.REQUESTED, W.UNDER_REVIEW, W.APPROVED))
        _release(request, "cancelled", member)
        _transition(request, W.CANCELLED, "cancelled", member, reason_code="member_request", decided_by=member,
                    finished_at=timezone.now(), release_entry=request.release_entry)
        _notify_member(request, "cancelled")
    return request


def _cash_available():
    return ledger.get_platform_account("CASH").debit_balance


def claim(actor, request, *, expected_status=W.APPROVED):
    """Exclusive processing claim: exactly one operator can hold it (row lock + expected state)."""
    _require(can_process, actor, "payout.claim", request)
    if not requests_active():
        raise WithdrawalFlowError(_("New payout processing is switched off for this deployment."), code="paused")
    with transaction.atomic():
        request = _locked(request)
        _expect(request, expected_status, (W.APPROVED,))
        if not current_settings().payouts_open:
            raise WithdrawalFlowError(_("New payout processing is paused."), code="paused")
        if actor.pk in (request.approved_by_id, request.member_id):
            raise NotAuthorized(_("The approver cannot also send the payout."), code="separation_of_duties")
        if _wallet_restricted(request):
            raise WithdrawalFlowError(_("The member's wallet is restricted. Do not send money."),
                                      code="wallet_restricted")
        if _cash_available() < request.net_payout:
            raise WithdrawalFlowError(_("Recorded cash is not enough for this payout. Check payout liquidity first."),
                                      code="insufficient_liquidity")
        return _transition(request, W.PAYOUT_PENDING, "claimed", actor, claimed_by=actor, claimed_at=timezone.now())


def reveal_destination(actor, request):
    """The full payout number, only for the operator holding the claim (audited every time)."""
    request = WithdrawalRequest.objects.get(pk=request.pk)  # Decide on the stored state, never a stale copy.
    if not (can_process(actor) and request.status == W.PAYOUT_PENDING and request.claimed_by_id == actor.pk):
        record_security_event("withdrawals.destination.revealed", outcome=DENIED, actor=actor, target=request,
                              metadata={"reference": request.reference})
        raise NotAuthorized(_("Only the operator processing this payout can see the full number."),
                            code="not_claimed")
    number = protect.decrypt(request.destination_encrypted)
    record_security_event("withdrawals.destination.revealed", outcome=SUCCESS, actor=actor, target=request,
                          metadata={"reference": request.reference})
    return number


def normalize_reference(raw):
    text = " ".join(str(raw or "").split()).upper()
    if not text or len(text) > 40 or not REFERENCE_RE.match(text):
        raise ValidationError({"payout_reference": _(
            "Enter the transaction reference exactly as the provider or bank shows it (letters and digits, up to 40 "
            "characters).")})
    key = re.sub(r"[^A-Z0-9]", "", text)
    if len(key) < 4:
        raise ValidationError({"payout_reference": _("This transaction reference is too short.")})
    return text, key


def record_payment(actor, request, *, payout_reference, evidence):
    """The operator who sent the money records the external reference and evidence."""
    _require(can_process, actor, "payout.record", request)
    text, key = normalize_reference(payout_reference)
    evidence = _reason_text(evidence)
    with transaction.atomic():
        request = _locked(request)
        _expect(request, "", (W.PAYOUT_PENDING,))
        if request.claimed_by_id != actor.pk:
            raise NotAuthorized(_("Only the operator processing this payout can record it."), code="not_claimed")
        lock = f"{request.method}:{key}"
        if (WithdrawalRequest.objects.filter(payout_reference_lock=lock).exclude(pk=request.pk).exists()
                or JournalEntry.objects.filter(source_type=SOURCE_PAYOUT, external_reference=lock).exists()):
            raise ValidationError({"payout_reference": _("This transaction reference is already recorded for "
                                                         "another payout.")})
        try:
            with transaction.atomic():
                _transition(request, W.VERIFYING, "payment_recorded", actor, payout_reference=text,
                            payout_reference_lock=lock, payout_evidence=evidence, recorded_at=timezone.now())
        except IntegrityError:
            raise ValidationError({"payout_reference": _("This transaction reference is already recorded for "
                                                         "another payout.")}) from None
        _notify_staff("withdrawals.verify_payouts", "verification_needed", request.pk,
                      N_("A payout awaits verification"),
                      N_("Withdrawal {reference} was sent; a second person must confirm the payment."),
                      "administration:withdrawal_request", reference=request.reference, exclude=(actor.pk,))
    return request


def report_problem(actor, request, *, note):
    """Outcome unknown (timeout, no confirmation, possible failure): the money stays reserved."""
    if not (can_process(actor) or can_verify_payouts(actor)):
        _require(lambda user: False, actor, "payout.problem", request)
    note = _reason_text(note)
    with transaction.atomic():
        request = _locked(request)
        _expect(request, "", (W.PAYOUT_PENDING, W.VERIFYING))
        if request.status == W.PAYOUT_PENDING and request.claimed_by_id != actor.pk and not can_verify_payouts(actor):
            raise NotAuthorized(_("Only the operator processing this payout can report it."), code="not_claimed")
        _transition(request, W.RECONCILIATION, "reconciliation", actor, note=note)
    return request


def verify_payment(actor, request, *, confirmed, note=""):
    """A different person confirms the payment in the provider or bank records; posts the payout.
    Allowed while the wallet is restricted: the money has already been sent and must be recorded."""
    _require(can_verify_payouts, actor, "payout.verify", request)
    if not confirmed:
        raise ValidationError({"confirmed": _("Confirm that you found this payment in the provider or bank records.")})
    if not settlement_active():
        raise WithdrawalFlowError(_("Withdrawal settlement is not approved for this deployment."), code="locked")
    with transaction.atomic():
        request = _locked(request)
        if request.status == W.PAID:
            return request
        _expect(request, "", (W.VERIFYING, W.RECONCILIATION))
        if not request.payout_reference_lock:
            raise WithdrawalFlowError(_("No payment has been recorded for this withdrawal."), code="no_payment")
        if actor.pk in (request.claimed_by_id, request.member_id):
            raise NotAuthorized(_("A different person must verify the payment."), code="separation_of_duties")
        # A restriction stops NEW money movements (approval, claim). It never blocks recording money that
        # has already left ClipCash: the reserved funds are settled to the verified outcome, and the
        # restriction is noted so the wallet review sees it.
        if _wallet_restricted(request):
            note = (f"{note} " if note else "") + "[Wallet restricted when the sent payment was verified.]"
        lines = [ledger.debit(request.wallet.reserved_account, request.wallet_deduction, "Withdrawal paid"),
                 ledger.credit(ledger.get_platform_account("CASH"), request.net_payout, "Paid to member")]
        if request.fee:
            lines.append(ledger.credit(ledger.get_platform_account("OTHER_OPERATING_REVENUE"), request.fee,
                                       "Withdrawal fee"))
        entry = ledger.post_entry(
            "withdrawal.paid_manual", lines=lines, idempotency_key=f"withdrawal-paid:{request.pk}",
            source_type=SOURCE_PAYOUT, source_id=str(request.pk), external_reference=request.payout_reference_lock,
            description=f"Withdrawal {request.reference} paid ({request.method})", actor=None)
        _wallet_txn(request.wallet, entry, WalletTransaction.Category.WITHDRAWAL_SETTLEMENT,
                    WalletTransaction.Bucket.RESERVED, WalletTransaction.Direction.OUT, request.wallet_deduction)
        _transition(request, W.PAID, "paid", actor, note=note, verified_by=actor, verified_at=timezone.now(),
                    payout_entry=entry, finished_at=timezone.now())
        _notify_member(request, "paid")
    return request


def confirm_failed(actor, request, *, note):
    """A verifier (not the operator) confirms no money left ClipCash: the reservation is released."""
    _require(can_verify_payouts, actor, "payout.failed", request)
    note = _reason_text(note)
    if not settlement_active():
        raise WithdrawalFlowError(_("Withdrawal settlement is not approved for this deployment."), code="locked")
    with transaction.atomic():
        request = _locked(request)
        _expect(request, "", (W.PAYOUT_PENDING, W.VERIFYING, W.RECONCILIATION))
        if actor.pk in (request.claimed_by_id, request.member_id):
            raise NotAuthorized(_("A different person must confirm a failed payout."), code="separation_of_duties")
        _release(request, "failed", actor)
        _transition(request, W.FAILED, "failed", actor, note=note, reason_code="payout_failed", decided_by=actor,
                    finished_at=timezone.now(), release_entry=request.release_entry)
        _notify_member(request, "failed")
    return request


# --- Reconciliation ---------------------------------------------------------------------------------


def reconcile(now=None):
    """Background job body: compare open reservations with the ledger and flag stuck payouts.
    Findings are audited; nothing is changed automatically (an uncertain payout is never retried)."""
    from datetime import timedelta

    from apps.wallets.wallet_services import account_balances

    now = now or timezone.now()
    findings = []
    open_rows = WithdrawalRequest.objects.filter(reserve_entry__isnull=False, release_entry__isnull=True,
                                                 payout_entry__isnull=True)
    expected = dict(open_rows.values_list("wallet_id").annotate(t=Sum("wallet_deduction")))
    wallets = Wallet.objects.filter(Q(pk__in=list(expected)) | Q(reserved_account__lines__isnull=False)).distinct()
    balances = account_balances([w.reserved_account_id for w in wallets])
    for wallet in wallets:
        ledger_total = balances.get(wallet.reserved_account_id, 0)
        if ledger_total != expected.get(wallet.pk, 0):
            findings.append(("reserved_mismatch", f"wallet {wallet.number}: ledger {ledger_total} vs "
                                                  f"requests {expected.get(wallet.pk, 0)}"))
    for request in WithdrawalRequest.objects.filter(status=W.PAID).select_related("payout_entry"):
        entry = request.payout_entry
        if entry is None or entry.posting_type != "withdrawal.paid_manual" or entry.total_amount != request.wallet_deduction:
            findings.append(("paid_without_matching_posting", request.reference))
    stuck_before = now - timedelta(minutes=settings.WITHDRAWALS_PAYOUT_STUCK_MINUTES)
    stuck = WithdrawalRequest.objects.filter(status=W.PAYOUT_PENDING, claimed_at__lt=stuck_before)
    for request in stuck:
        findings.append(("payout_claim_open", request.reference))
    if findings:
        with transaction.atomic():
            for code, detail in findings:
                record_business_event("withdrawals.reconciliation.discrepancy", actor=None,
                                      metadata={"code": code, "detail": detail[:200]})
        _notify_staff("withdrawals.verify_payouts", "reconciliation_findings", now.date().isoformat(),
                      N_("Withdrawal reconciliation needs attention"),
                      N_("Some withdrawals need a check: open payout claims or reservation differences."),
                      "administration:withdrawal_requests")
    return findings


def summary():
    rows = dict(WithdrawalRequest.objects.values_list("status").annotate(n=Count("pk")))
    reserved = WithdrawalRequest.objects.filter(status__in=OPEN_WITHDRAWAL_STATES).aggregate(
        t=Coalesce(Sum("wallet_deduction"), 0))["t"]
    paid = WithdrawalRequest.objects.filter(status=W.PAID).aggregate(t=Coalesce(Sum("net_payout"), 0))["t"]
    return {"counts": rows, "reserved": reserved, "paid": paid,
            "awaiting_review": rows.get(W.REQUESTED, 0) + rows.get(W.UNDER_REVIEW, 0),
            "closed": rows.get(W.REJECTED, 0) + rows.get(W.CANCELLED, 0) + rows.get(W.FAILED, 0)}


# --- Notifications -----------------------------------------------------------------------------------

MEMBER_NOTICES = {
    "requested": (N_("Withdrawal requested: TZS {amount}"),
                  N_("We reserved TZS {deduction} from your available balance for withdrawal {reference}. You will "
                     "receive TZS {net} at {destination} once it is paid.")),
    "info_requested": (N_("More information needed for withdrawal {reference}"),
                       N_("ClipCash needs more information: {note}")),
    "approved": (N_("Withdrawal {reference} approved"),
                 N_("Your withdrawal of TZS {net} to {destination} is approved and waiting to be sent.")),
    "rejected": (N_("Withdrawal {reference} not approved"),
                 N_("TZS {deduction} is back in your available balance. Reason: {reason__t}")),
    "cancelled": (N_("Withdrawal {reference} cancelled"), N_("TZS {deduction} is back in your available balance.")),
    "failed": (N_("Withdrawal {reference} could not be paid"),
               N_("The transfer did not go through. TZS {deduction} is back in your available balance.")),
    "paid": (N_("Withdrawal {reference} paid: TZS {net}"),
             N_("TZS {net} was sent to {destination} (transaction {transaction}).")),
}


def _notify_member(request, event):
    from apps.notifications import inbox

    title, message = MEMBER_NOTICES[event]
    inbox.notify(request.member, f"withdrawals.real_{event}", category=inbox.Category.WITHDRAWAL, title=title,
                 message=message, params={
                     "reference": request.reference, "amount": f"{request.amount:,}",
                     "deduction": f"{request.wallet_deduction:,}", "net": f"{request.net_payout:,}",
                     "destination": request.destination_label, "note": request.info_request or "",
                     "reason__t": REASON_TEXT.get(request.reason_code, ""), "transaction": request.payout_reference},
                 link=inbox.link_to("withdrawals:request", request.pk),
                 dedupe_key=f"withdrawals.real_{event}:{request.pk}:{request.updated_at.isoformat()}",
                 email=event in ("paid", "rejected", "failed"))


def _notify_staff(perm, event, key, title, message, url_name, *, exclude=(), **params):
    from apps.notifications import inbox

    staff = [user for user in inbox.staff_with_permission(perm) if user.pk not in exclude]
    args = (key,) if url_name.endswith(("_request", "_identity")) else ()
    inbox.notify(staff, f"withdrawals.{event}", category=inbox.Category.WITHDRAWAL, title=title, message=message,
                 params=params, link=inbox.link_to(url_name, *args), dedupe_key=f"withdrawals.{event}:{key}")


REASON_MESSAGES = {code: gettext_lazy(text) for code, text in REASON_TEXT.items()}
