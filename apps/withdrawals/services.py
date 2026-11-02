"""
Withdrawal services (Phase 10), DEMONSTRATION ONLY.

* Real withdrawals: ``withdrawal_eligibility`` always denies (financial feature
  "withdrawals" is locked, wallets hold TZS 0 and the Phase 09 posting lock
  stands); ``request_withdrawal`` always refuses; the database refuses any
  WithdrawalRequest row. Eligibility never reads demo rewards or sandbox deposits.
* Demo withdrawals: staff-run simulations in their own table with DEMO_* states.
  Separation of duties (creator != approver(s) != payout initiator), stale-decision
  protection, immutable decision history. A simulated payout never touches the
  ledger, wallets, the sandbox journal or any external system.
* Payout notices are authenticated, de-duplicated and verified against the
  provider's own record. A timeout means "status unknown", never "failed", and an
  uncertain payout is reconciled with the same reference, never re-sent.
"""

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.crypto import salted_hmac

from apps.accounts.phone_numbers import normalize_phone_number
from apps.audit.services import DENIED, record_business_event, record_security_event
from apps.core import features, ratelimit
from apps.core.money import to_amount
from apps.payments.providers import sandbox_active
from apps.wallets import wallet_services
from apps.wallets.models import WalletRestriction

from .models import (
    DemoWithdrawal,
    DemoWithdrawalDecision,
    PayoutBeneficiary,
    PayoutEvent,
    SandboxPayout,
    WithdrawalException,
)
from .payouts import CallbackRejected, PayoutTimeout, get_payout_provider

D = DemoWithdrawal.Status
B = PayoutBeneficiary
OPEN_DEMO = (D.REQUESTED, D.UNDER_REVIEW, D.APPROVED, D.PAYOUT_PENDING, D.UNCERTAIN)
COUNTED_DEMO = OPEN_DEMO + (D.PAID,)
REJECTION_REASONS = ("beneficiary_mismatch", "suspected_fraud", "limit_exceeded", "member_request",
                     "insufficient_evidence", "duplicate_request", "other")
MIN_NOTE = 5


class WithdrawalError(Exception):
    code = "withdrawal_error"

    def __init__(self, message, *, code=None):
        super().__init__(message)
        if code:
            self.code = code


class NotAuthorized(WithdrawalError):
    code = "not_authorized"


class NotFound(WithdrawalError):
    code = "not_found"


class InvalidState(WithdrawalError):
    code = "invalid_state"


class StaleDecision(WithdrawalError):
    code = "stale"


class WithdrawalsLocked(WithdrawalError):
    code = "withdrawals_locked"


# --- Permissions -----------------------------------------------------------------------------


def _staff_with(user, perm):
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.is_active
                and user.is_staff and user.has_perm(perm))


def can_view(user):
    return _staff_with(user, "withdrawals.view_withdrawals")


def can_review(user):
    return _staff_with(user, "withdrawals.review_withdrawals")


def can_operate_payouts(user):
    return _staff_with(user, "withdrawals.operate_payout_sandbox")


def can_view_beneficiaries(user):
    return _staff_with(user, "withdrawals.view_beneficiaries")


def _deny(action, actor, target=None, reason="permission_missing"):
    record_security_event("withdrawals.action.denied", outcome=DENIED,
                          actor=actor if getattr(actor, "is_authenticated", False) else None,
                          target=target, metadata={"action": action, "reason": reason})
    raise NotAuthorized("You are not allowed to do this.", code=reason)


def _note(text, *, required=True):
    text = (text or "").strip()
    if required and len(text) < MIN_NOTE:
        raise ValidationError({"note": "Please give a reason (at least 5 characters)."})
    return text[:300]


# --- Beneficiaries ------------------------------------------------------------------------------


def _fingerprint(kind, provider, identifier):
    return salted_hmac("clipcash.withdrawals.beneficiary", f"{kind}:{provider}:{identifier}",
                       algorithm="sha256").hexdigest()


def _clean_destination(kind, network, bank_code, identifier):
    """(provider, normalized identifier). The identifier itself is never stored."""
    errors = {}
    provider = ""
    if kind == B.Kind.MOBILE_MONEY:
        if network not in B.Network.values:
            errors["network"] = "Choose a mobile network."
        try:
            identifier = normalize_phone_number(identifier)
        except ValidationError:
            identifier = None
        if not identifier or not identifier.startswith("+255"):
            errors["identifier"] = "Enter a Tanzanian mobile number, for example 0712345678."
        provider = network
    elif kind == B.Kind.BANK:
        bank_code = (bank_code or "").strip().upper()
        if not (2 <= len(bank_code) <= 12 and bank_code.isalnum()):
            errors["bank_code"] = "Enter the bank code (2-12 letters or digits)."
        identifier = (identifier or "").replace(" ", "").replace("-", "")
        if not (identifier.isdigit() and 6 <= len(identifier) <= 20):
            errors["identifier"] = "Enter the account number (6-20 digits)."
        provider = bank_code
    else:
        errors["kind"] = "Choose mobile money or bank."
    if errors:
        raise ValidationError(errors)
    return provider, identifier


def _member_ready(member):
    return wallet_services.wallet_eligibility(member) == "ok"


def add_beneficiary(member, *, kind, holder_name, identifier, network="", bank_code=""):
    """Validate a destination, then store only a masked form and a keyed fingerprint.
    Never pass PINs, OTPs or passwords: the form has no such fields."""
    if not _member_ready(member):
        _deny("beneficiary.add", member, reason="not_eligible")
    holder_name = " ".join((holder_name or "").split())[:100]
    if len(holder_name) < 2:
        raise ValidationError({"holder_name": "Enter the account holder's name."})
    provider, normalized = _clean_destination(kind, network, bank_code, identifier)
    # Only well-formed attempts count, so typing mistakes do not use up the allowance.
    key = hashlib.sha256(f"withdrawals-beneficiary:{member.pk}".encode()).hexdigest()
    if ratelimit.hit(key, 86_400) > settings.WITHDRAWALS_BENEFICIARY_CHANGES_PER_DAY:
        raise InvalidState("Too many beneficiary changes today. Please try again tomorrow.", code="rate_limited")
    digits = "".join(ch for ch in normalized if ch.isdigit())
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=member.pk)
        active = PayoutBeneficiary.objects.filter(member=member, status=B.Status.ACTIVE)
        if active.count() >= settings.WITHDRAWALS_MAX_BENEFICIARIES:
            raise InvalidState("You can keep at most three payout destinations. Remove one first.",
                               code="too_many")
        fingerprint = _fingerprint(kind, provider, normalized)
        if active.filter(kind=kind, identifier_fingerprint=fingerprint).exists():
            raise ValidationError({"identifier": "This destination is already saved."})
        beneficiary = PayoutBeneficiary.objects.create(
            member=member, kind=kind, network=network if kind == B.Kind.MOBILE_MONEY else "",
            bank_code=provider if kind == B.Kind.BANK else "", holder_name=holder_name,
            identifier_last4=digits[-4:], identifier_fingerprint=fingerprint)
        record_business_event("withdrawals.beneficiary.created", actor=member, target=beneficiary,
                              metadata={"kind": kind, "provider": provider})
    return beneficiary


def beneficiary_for(member, beneficiary_id):
    beneficiary = PayoutBeneficiary.objects.filter(pk=beneficiary_id, member=member).first()
    if beneficiary is None:
        raise NotFound("Payout destination not found.")
    return beneficiary


def deactivate_beneficiary(member, beneficiary_id):
    with transaction.atomic():
        beneficiary = (PayoutBeneficiary.objects.select_for_update()
                       .filter(pk=beneficiary_id, member=member).first())
        if beneficiary is None:
            raise NotFound("Payout destination not found.")
        if beneficiary.status == B.Status.DEACTIVATED:
            return beneficiary
        beneficiary.status = B.Status.DEACTIVATED
        beneficiary.deactivated_at = timezone.now()
        beneficiary.save()
        record_business_event("withdrawals.beneficiary.deactivated", actor=member, target=beneficiary,
                              metadata={"kind": beneficiary.kind})
    return beneficiary


def demo_verify_beneficiary(member, beneficiary_id):
    """DEMO check only (sandbox provider). Never claims a real account verification."""
    provider = get_payout_provider()
    beneficiary = beneficiary_for(member, beneficiary_id)
    if beneficiary.status != B.Status.ACTIVE:
        raise InvalidState("Removed destinations cannot be checked.")
    ok, _reason = provider.validate_beneficiary(beneficiary)
    if not ok:
        raise InvalidState("The demo check did not pass.")
    with transaction.atomic():
        beneficiary = PayoutBeneficiary.objects.select_for_update().get(pk=beneficiary.pk)
        if beneficiary.verification != B.Verification.DEMO_VERIFIED:
            beneficiary.verification = B.Verification.DEMO_VERIFIED
            beneficiary.verified_at = timezone.now()
            beneficiary.save()
            record_business_event("withdrawals.beneficiary.demo_verified", actor=member, target=beneficiary,
                                  metadata={"kind": beneficiary.kind})
    return beneficiary


# --- Real withdrawals: eligibility (always denied in Phase 10) ----------------------------------


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reasons: list = field(default_factory=list)
    available: int = 0  # Ledger-backed available balance (never demo or sandbox amounts).

    def __bool__(self):
        return self.eligible


ELIGIBILITY_MESSAGES = {
    "withdrawals_locked": "Withdrawals are not available yet. Real rewards and payouts are not active.",
    "not_eligible_account": "Only verified, active member accounts can withdraw.",
    "no_wallet": "Open your wallet first.",
    "wallet_restricted": "Your wallet is under review.",
    "insufficient_balance": "Your available wallet balance is not enough.",
    "no_verified_beneficiary": "Add a verified payout destination.",
    "beneficiary_cooling_off": "New payout destinations can be used after 24 hours.",
    "below_minimum": "The amount is below the minimum withdrawal.",
    "above_maximum": "The amount is above the maximum withdrawal.",
    "daily_limit": "The amount would exceed your daily limit.",
    "weekly_limit": "The amount would exceed your weekly limit.",
    "frequency_limit": "Too many withdrawal requests today.",
}


def _amount_or_none(amount):
    if amount in (None, ""):
        return None
    try:
        value = to_amount(amount)
    except (ValueError, TypeError):
        raise ValidationError({"amount": "Enter a whole number of shillings."}) from None
    if value <= 0:
        raise ValidationError({"amount": "Enter an amount greater than zero."})
    return value


def withdrawal_eligibility(member, *, amount=None, beneficiary=None):
    """Centralized REAL withdrawal eligibility with stable reason codes. Always
    denied in this phase; every other rule is evaluated so members see the full picture."""
    amount = _amount_or_none(amount)
    reasons = []
    if not features.is_enabled("withdrawals"):  # Locked financial feature: always False.
        reasons.append("withdrawals_locked")
    if not _member_ready(member):
        return Eligibility(False, reasons + ["not_eligible_account"])
    wallet = wallet_services.wallet_for(member)
    available = 0
    if wallet is None:
        reasons.append("no_wallet")
    else:
        balances = wallet_services.balances(wallet)  # Ledger projection only.
        available = balances.spendable
        if balances.restricted:
            reasons.append("wallet_restricted")
        if amount is None or available < amount or available == 0:
            reasons.append("insufficient_balance")
    usable = PayoutBeneficiary.objects.filter(member=member, status=B.Status.ACTIVE)
    if beneficiary is not None:
        usable = usable.filter(pk=beneficiary.pk)
    if not usable.exclude(verification=B.Verification.UNVERIFIED).exists():
        reasons.append("no_verified_beneficiary")
    elif not usable.filter(created_at__lte=timezone.now()
                           - timedelta(hours=settings.WITHDRAWALS_BENEFICIARY_COOLING_HOURS)).exists():
        reasons.append("beneficiary_cooling_off")
    if amount is not None:
        reasons += _limit_reasons(member, amount, model="real")
    return Eligibility(False, sorted(set(reasons), key=reasons.index), available=available)


def _limit_reasons(member, amount, *, model):
    reasons = []
    if amount < settings.WITHDRAWALS_MIN_AMOUNT:
        reasons.append("below_minimum")
    if amount > settings.WITHDRAWALS_MAX_AMOUNT:
        reasons.append("above_maximum")
    if model == "demo":
        now = timezone.now()
        counted = DemoWithdrawal.objects.filter(member=member, status__in=COUNTED_DEMO)
        day = counted.filter(created_at__gte=now - timedelta(days=1))
        week = counted.filter(created_at__gte=now - timedelta(days=7))
        if day.aggregate(t=Sum("simulated_amount", default=0))["t"] + amount > settings.WITHDRAWALS_DAILY_LIMIT:
            reasons.append("daily_limit")
        if week.aggregate(t=Sum("simulated_amount", default=0))["t"] + amount > settings.WITHDRAWALS_WEEKLY_LIMIT:
            reasons.append("weekly_limit")
        if DemoWithdrawal.objects.filter(member=member, created_at__gte=now - timedelta(days=1)).count() \
                >= settings.WITHDRAWALS_MAX_REQUESTS_PER_DAY:
            reasons.append("frequency_limit")
    return reasons


def request_withdrawal(member, **kwargs):
    """The REAL request entry point. Always refused in Phase 10 (and the database
    refuses WithdrawalRequest rows)."""
    decision = withdrawal_eligibility(member, amount=kwargs.get("amount"), beneficiary=kwargs.get("beneficiary"))
    record_security_event("withdrawals.request.denied", outcome=DENIED,
                          actor=member if getattr(member, "is_authenticated", False) else None,
                          metadata={"reasons": decision.reasons})
    raise WithdrawalsLocked("Withdrawals are not available yet.", code="withdrawals_locked")


def demo_preview(member, *, amount):
    """Read-only preview of what a withdrawal WOULD require. Nothing is stored."""
    amount = _amount_or_none(amount)
    real = withdrawal_eligibility(member, amount=amount)
    return {"amount": amount, "reasons": real.reasons,
            "messages": [ELIGIBILITY_MESSAGES.get(code, code) for code in real.reasons],
            "limits": _limit_reasons(member, amount, model="real") if amount else [],
            "high_risk": bool(amount and amount >= settings.WITHDRAWALS_HIGH_RISK_AMOUNT)}


# --- Demo withdrawals (staff-run simulations) -----------------------------------------------------


def _new_reference():
    return "DW-" + secrets.token_hex(6).upper()


def _decide(withdrawal, action, from_status, *, actor=None, reason_code="", note=""):
    DemoWithdrawalDecision.objects.create(withdrawal=withdrawal, action=action, from_status=from_status,
                                          to_status=withdrawal.status, actor=actor, reason_code=reason_code,
                                          note=note[:300])


def _risk_flags(member, beneficiary, amount):
    now = timezone.now()
    flags = []
    if beneficiary.created_at > now - timedelta(hours=settings.WITHDRAWALS_BENEFICIARY_COOLING_HOURS):
        flags.append("new_beneficiary")
    wallet = wallet_services.wallet_for(member)
    if wallet and WalletRestriction.objects.filter(wallet=wallet, created_at__gte=now - timedelta(days=30)).exists():
        flags.append("recent_wallet_restriction")
    if DemoWithdrawal.objects.filter(member=member, status__in=[D.FAILED, D.RETURNED],
                                     created_at__gte=now - timedelta(days=30)).count() >= 2:
        flags.append("repeated_payout_failures")
    if PayoutBeneficiary.objects.filter(member=member, created_at__gte=now - timedelta(days=7)).count() >= 3:
        flags.append("frequent_beneficiary_changes")
    if beneficiary.verification == B.Verification.UNVERIFIED:
        flags.append("unverified_beneficiary")
    return flags


def create_demo_withdrawal(actor, member, beneficiary, *, amount, idempotency_key):
    """Staff create a SIMULATED scenario for a member. No real obligation exists."""
    get_payout_provider()  # Sandbox gate.
    if not can_review(actor):
        _deny("demo.create", actor, member)
    if actor.pk == member.pk:
        _deny("demo.create", actor, member, "self_service")
    key = str(idempotency_key or "").strip()
    if not 8 <= len(key) <= 64:
        raise ValidationError({"idempotency_key": "Missing form token. Reload and try again."})
    existing = DemoWithdrawal.objects.filter(created_by=actor, idempotency_key=key).first()
    if existing is not None:
        return existing
    amount = _amount_or_none(amount)
    if amount is None:
        raise ValidationError({"amount": "Enter a simulated amount."})
    if not _member_ready(member):
        raise InvalidState("The member's account is not eligible.", code="not_eligible_account")
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=member.pk)  # Serialize per member.
        beneficiary = PayoutBeneficiary.objects.filter(pk=beneficiary.pk, member=member,
                                                       status=B.Status.ACTIVE).first()
        if beneficiary is None:
            raise NotFound("Choose an active payout destination of this member.")
        limits = _limit_reasons(member, amount, model="demo")
        if DemoWithdrawal.objects.filter(member=member, status__in=OPEN_DEMO).exists():
            limits.append("open_request_exists")
        if limits:
            refusal = limits
        else:
            refusal = None
            flags = _risk_flags(member, beneficiary, amount)
            withdrawal = DemoWithdrawal.objects.create(
                reference=_new_reference(), member=member, beneficiary=beneficiary,
                destination_kind=beneficiary.kind,
                destination_provider=beneficiary.get_network_display() if beneficiary.kind == B.Kind.MOBILE_MONEY
                else beneficiary.bank_code,
                destination_last4=beneficiary.identifier_last4,
                destination_fingerprint=beneficiary.identifier_fingerprint,
                destination_holder=beneficiary.holder_name, simulated_amount=amount,
                high_risk=bool(flags) or amount >= settings.WITHDRAWALS_HIGH_RISK_AMOUNT, flags=flags,
                idempotency_key=key, created_by=actor)
            _decide(withdrawal, "created", "", actor=actor)
            record_business_event("withdrawals.demo.created", actor=actor, target=withdrawal, metadata={
                "reference": withdrawal.reference, "amount": amount, "high_risk": withdrawal.high_risk,
                "flags": flags})
            if flags:
                record_security_event("withdrawals.risk.flagged", outcome=DENIED, actor=None, target=withdrawal,
                                      metadata={"flags": flags})
    if refusal:
        record_security_event("withdrawals.demo.duplicate_refused" if "open_request_exists" in refusal
                              else "withdrawals.action.denied", outcome=DENIED, actor=actor, target=member,
                              metadata={"reason": refusal[0]} if "open_request_exists" in refusal
                              else {"action": "demo.create", "reason": refusal[0]})
        raise InvalidState("This scenario breaks the withdrawal rules: " + ", ".join(refusal) + ".",
                           code=refusal[0])
    return withdrawal


def _locked(withdrawal):
    return DemoWithdrawal.objects.select_for_update().get(pk=withdrawal.pk)


def _check_stale(current, expected_status):
    if current.status != expected_status:
        raise StaleDecision("This withdrawal changed since you opened it. Reload and review again.")


def start_review(actor, withdrawal, *, expected_status):
    if not can_review(actor):
        _deny("demo.review", actor, withdrawal)
    with transaction.atomic():
        current = _locked(withdrawal)
        if current.status == D.UNDER_REVIEW and expected_status == D.UNDER_REVIEW:
            return current
        _check_stale(current, expected_status)
        if current.status != D.REQUESTED:
            raise InvalidState("Only new requests can be taken into review.")
        current.status = D.UNDER_REVIEW
        current.reviewed_at = timezone.now()
        current.save()
        _decide(current, "review_started", D.REQUESTED, actor=actor)
        record_business_event("withdrawals.demo.review_started", actor=actor, target=current,
                              metadata={"reference": current.reference})
    return current


def approve(actor, withdrawal, *, expected_status, note=""):
    """Approver != creator. High-risk scenarios need two distinct approvers."""
    if not can_review(actor):
        _deny("demo.approve", actor, withdrawal)
    current = DemoWithdrawal.objects.get(pk=withdrawal.pk)
    if actor.pk in (current.created_by_id, current.member_id):
        _deny("demo.approve", actor, current, "self_approval")
    if actor.pk == current.approved_by_id:
        _deny("demo.approve", actor, current, "second_approver_required")
    with transaction.atomic():
        current = _locked(withdrawal)
        _check_stale(current, expected_status)
        if current.status != D.UNDER_REVIEW:
            raise InvalidState("Only requests under review can be approved.")
        if actor.pk in (current.created_by_id, current.approved_by_id):
            raise NotAuthorized("A different reviewer must approve.", code="self_approval")
        now = timezone.now()
        if current.approved_by_id is None:
            current.approved_by = actor
            if not current.high_risk:
                current.status = D.APPROVED
                current.approved_at = now
        else:
            current.second_approved_by = actor
            current.status = D.APPROVED
            current.approved_at = now
        current.save()
        approvals = 2 if current.second_approved_by_id else 1
        _decide(current, "approved" if current.status == D.APPROVED else "approval_recorded", D.UNDER_REVIEW,
                actor=actor, note=note)
        record_business_event("withdrawals.demo.approved" if current.status == D.APPROVED
                              else "withdrawals.demo.approval_recorded", actor=actor, target=current,
                              metadata={"reference": current.reference, **({} if current.status == D.APPROVED
                                                                          else {"approvals": approvals})})
    return current


def reject(actor, withdrawal, *, expected_status, reason_code, note=""):
    if not can_review(actor):
        _deny("demo.reject", actor, withdrawal)
    if reason_code not in REJECTION_REASONS:
        raise ValidationError({"reason_code": "Choose a reason."})
    with transaction.atomic():
        current = _locked(withdrawal)
        if current.status == D.REJECTED:
            return current
        _check_stale(current, expected_status)
        if current.status not in (D.REQUESTED, D.UNDER_REVIEW, D.APPROVED):
            raise InvalidState("Only requests that have not been paid out can be rejected.")
        previous = current.status
        current.status = D.REJECTED
        current.reason_code = reason_code
        current.finished_at = timezone.now()
        current.save()
        _decide(current, "rejected", previous, actor=actor, reason_code=reason_code, note=note)
        record_business_event("withdrawals.demo.rejected", actor=actor, target=current,
                              metadata={"reference": current.reference, "reason_code": reason_code})
    return current


def cancel(actor, withdrawal, *, reason_code="member_request", note=""):
    if not can_review(actor):
        _deny("demo.cancel", actor, withdrawal)
    with transaction.atomic():
        current = _locked(withdrawal)
        if current.status == D.CANCELLED:
            return current  # Duplicate cancellation: no new decision.
        if current.status not in (D.REQUESTED, D.UNDER_REVIEW, D.APPROVED):
            raise InvalidState("A payout already started; it cannot be cancelled.")
        previous = current.status
        current.status = D.CANCELLED
        current.reason_code = reason_code if reason_code in REJECTION_REASONS else "other"
        current.finished_at = timezone.now()
        current.save()
        _decide(current, "cancelled", previous, actor=actor, reason_code=current.reason_code, note=note)
        record_business_event("withdrawals.demo.cancelled", actor=actor, target=current,
                              metadata={"reference": current.reference, "reason_code": current.reason_code})
    return current


def initiate_payout(actor, withdrawal, *, expected_status=D.APPROVED):
    """Start the SIMULATED payout. The initiator must not be an approver."""
    provider = get_payout_provider()
    if not can_operate_payouts(actor):
        _deny("payout.initiate", actor, withdrawal)
    current = DemoWithdrawal.objects.get(pk=withdrawal.pk)
    if actor.pk in (current.approved_by_id, current.second_approved_by_id):
        _deny("payout.initiate", actor, current, "approver_cannot_execute")
    with transaction.atomic():
        current = _locked(withdrawal)
        if current.status in (D.PAYOUT_PENDING, D.UNCERTAIN) and expected_status == current.status:
            return current
        _check_stale(current, expected_status)
        if current.status != D.APPROVED:
            raise InvalidState("Only approved requests can be paid out.")
        current.status = D.PAYOUT_PENDING
        current.payout_reference = "DPO-" + secrets.token_hex(8).upper()
        current.payout_initiated_by = actor
        current.payout_started_at = timezone.now()
        current.save()
        _decide(current, "payout_initiated", D.APPROVED, actor=actor)
        record_business_event("withdrawals.payout.initiated", actor=actor, target=current,
                              metadata={"reference": current.reference})
    try:  # Provider call outside the transaction; the reference is already fixed.
        provider.create_payout(current.payout_reference, current.reference, current.simulated_amount)
    except PayoutTimeout:
        _mark_uncertain(current.pk)
    return DemoWithdrawal.objects.get(pk=current.pk)


def _mark_uncertain(withdrawal_id):
    with transaction.atomic():
        current = DemoWithdrawal.objects.select_for_update().get(pk=withdrawal_id)
        if current.status == D.PAYOUT_PENDING:
            current.status = D.UNCERTAIN
            current.save()
            _decide(current, "payout_uncertain", D.PAYOUT_PENDING, note="Provider did not answer; not a failure.")
            record_business_event("withdrawals.payout.uncertain", actor=None, target=current,
                                  metadata={"reference": current.reference})


# --- Payout notices and reconciliation -------------------------------------------------------------


def receive_payout_callback(body, signature):
    """Authenticated provider notice -> (http status, outcome)."""
    provider = get_payout_provider()
    if not isinstance(body, bytes) or len(body) > settings.PAYMENTS_CALLBACK_MAX_BYTES:
        return 413, "too_large"
    try:
        notice = provider.validate_callback(body, signature)
    except CallbackRejected as error:
        PayoutEvent.objects.create(provider=provider.code, event_id="unauthenticated", authenticated=False,
                                   outcome="REFUSED", summary={"reason": error.reason})
        record_security_event("withdrawals.callback.refused", outcome=DENIED, actor=None,
                              metadata={"reason": error.reason})
        return 401, error.reason
    return 200, process_payout_update(provider, notice.event_id, notice.reference, kind="callback")


def process_payout_update(provider, event_id, reference, *, kind):
    try:
        status = provider.fetch_status(reference)  # No lock held.
    except PayoutTimeout:
        status = "unreachable"
    summary = ({"status": status.status} if hasattr(status, "status") else {"status": str(status)})
    duplicate = False
    with transaction.atomic():
        try:
            with transaction.atomic():
                event = PayoutEvent.objects.create(provider=provider.code, event_id=event_id, authenticated=True,
                                                   outcome="NO_CHANGE", payout_reference=reference, summary=summary)
        except IntegrityError:
            duplicate = True
        if duplicate:
            PayoutEvent.objects.create(provider=provider.code, event_id=event_id[:100], authenticated=False,
                                       outcome="DUPLICATE", payout_reference=reference)
        else:
            outcome = _apply_payout(status, reference)
            PayoutEvent.objects.filter(pk=event.pk).update(outcome=outcome)
    if duplicate:
        record_security_event("withdrawals.callback.duplicate", outcome=DENIED, actor=None, metadata={"kind": kind})
        return "DUPLICATE"
    return outcome


def _apply_payout(status, reference):
    current = DemoWithdrawal.objects.select_for_update().filter(payout_reference=reference).first()
    if current is None:
        _exception(None, "unknown_payout_reference", "A payout notice referred to an unknown reference.")
        return "EXCEPTION"
    if status == "unreachable":
        if current.status == D.PAYOUT_PENDING:
            current.status = D.UNCERTAIN
            current.save()
            _decide(current, "payout_uncertain", D.PAYOUT_PENDING, note="Provider unreachable; not a failure.")
            record_business_event("withdrawals.payout.uncertain", actor=None, target=current,
                                  metadata={"reference": current.reference})
        return "UNREACHABLE"
    if status is None or status.withdrawal_reference != current.reference or status.amount != current.simulated_amount:
        _exception(current, "payout_mismatch", "Provider record does not match the demo withdrawal.")
        return "EXCEPTION"
    previous = current.status
    if status.status == "SUCCEEDED" and current.status in (D.PAYOUT_PENDING, D.UNCERTAIN):
        current.status, event = D.PAID, "withdrawals.payout.completed"
    elif status.status == "FAILED" and current.status in (D.PAYOUT_PENDING, D.UNCERTAIN):
        current.status, event = D.FAILED, "withdrawals.payout.failed"
    elif status.status == "RETURNED" and current.status == D.PAID:
        current.status, event = D.RETURNED, "withdrawals.payout.returned"
    else:
        return "NO_CHANGE"
    current.finished_at = timezone.now()
    current.save()
    _decide(current, event.rsplit(".", 1)[1], previous)
    record_business_event(event, actor=None, target=current, metadata={"reference": current.reference})
    return "ACCEPTED"


def reconcile_payouts(limit=200):
    """Background job: ask the provider about pending/uncertain demo payouts (same reference)."""
    if not sandbox_active():
        return 0
    provider = get_payout_provider()
    pending = list(DemoWithdrawal.objects.filter(status__in=[D.PAYOUT_PENDING, D.UNCERTAIN])
                   .order_by("payout_started_at").values_list("payout_reference", flat=True)[:limit])
    for reference in pending:
        record = SandboxPayout.objects.filter(reference=reference).first()
        sequence = record.sequence if record else 0
        reachable = "u" if record and record.unreachable else "r"
        process_payout_update(provider, f"poll:{reference}:{sequence}:{reachable}", reference, kind="poll")
    return len(pending)


def _exception(withdrawal, code, detail):
    exists = WithdrawalException.objects.filter(withdrawal=withdrawal, code=code, status="OPEN")
    if exists.exists():
        return exists.first()
    exc = WithdrawalException.objects.create(withdrawal=withdrawal, code=code, detail=detail[:300])
    record_business_event("withdrawals.exception.opened", actor=None, target=withdrawal, metadata={"code": code})
    return exc


def detect_stuck(now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(minutes=settings.WITHDRAWALS_PAYOUT_STUCK_MINUTES)
    count = 0
    for withdrawal in DemoWithdrawal.objects.filter(status__in=[D.PAYOUT_PENDING, D.UNCERTAIN],
                                                    payout_started_at__lte=cutoff)[:200]:
        with transaction.atomic():
            _exception(withdrawal, "payout_stuck", "Payout has no final status. Reconcile; do not re-send.")
        count += 1
    return count


def expire_demo(now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(days=settings.WITHDRAWALS_DEMO_EXPIRY_DAYS)
    count = 0
    with transaction.atomic():
        for current in (DemoWithdrawal.objects.select_for_update(skip_locked=True)
                        .filter(status__in=[D.REQUESTED, D.UNDER_REVIEW], created_at__lte=cutoff)[:500]):
            previous = current.status
            current.status = D.EXPIRED
            current.finished_at = now
            current.save()
            _decide(current, "expired", previous)
            record_business_event("withdrawals.demo.expired", actor=None, target=current,
                                  metadata={"reference": current.reference})
            count += 1
    return count


def resolve_exception(actor, exception, *, note):
    if not can_review(actor):
        _deny("exception.resolve", actor)
    note = _note(note)
    with transaction.atomic():
        exception = WithdrawalException.objects.select_for_update().get(pk=exception.pk)
        if exception.status == WithdrawalException.Status.RESOLVED:
            return exception
        exception.status = WithdrawalException.Status.RESOLVED
        exception.resolved_by = actor
        exception.resolved_at = timezone.now()
        exception.resolution_note = note
        exception.save()
        record_business_event("withdrawals.exception.resolved", actor=actor, target=exception.withdrawal,
                              metadata={"code": exception.code})
    return exception


# --- Simulator ---------------------------------------------------------------------------------------

SIMULATOR_ACTIONS = ("succeed", "fail", "timeout", "restore", "return", "replay", "forged")


def simulate(actor, withdrawal, action):
    """Staff-only: plays the payout provider, then delivers a signed notice through the
    normal callback path. Never sets demo statuses directly."""
    provider = get_payout_provider()
    if not can_operate_payouts(actor):
        _deny("sandbox.simulate", actor, withdrawal)
    if action not in SIMULATOR_ACTIONS:
        raise InvalidState("Unknown simulator action.")
    withdrawal = DemoWithdrawal.objects.get(pk=withdrawal.pk)
    if not withdrawal.payout_reference:
        raise InvalidState("Start the payout first.")
    with transaction.atomic():
        record = SandboxPayout.objects.select_for_update().get(reference=withdrawal.payout_reference)
        record_business_event("withdrawals.sandbox.simulated", actor=actor, target=withdrawal,
                              metadata={"reference": withdrawal.reference, "action": action})
        if action == "succeed":
            record.status = SandboxPayout.Status.SUCCEEDED
        elif action == "fail":
            record.status = SandboxPayout.Status.FAILED
        elif action == "return":
            if record.status != SandboxPayout.Status.SUCCEEDED:
                raise InvalidState("Only completed payouts can be returned.")
            record.status = SandboxPayout.Status.RETURNED
        elif action == "timeout":
            record.unreachable = True
        elif action == "restore":
            record.unreachable = False
        if action not in ("replay", "forged"):
            record.sequence += 1
            record.save()
        sequence = record.sequence
    body = json.dumps({"event_id": f"{record.reference}:{sequence}", "reference": record.reference},
                      separators=(",", ":")).encode()
    signature = "0" * 64 if action == "forged" else provider.sign(body)
    return receive_payout_callback(body, signature)


# --- Queries -----------------------------------------------------------------------------------------


def demo_for_member(member):
    return DemoWithdrawal.objects.filter(member=member).order_by("-created_at")


def staff_queue(*, status="", query="", flagged=False):
    qs = DemoWithdrawal.objects.select_related("member").order_by("-created_at")
    if status in D.values:
        qs = qs.filter(status=status)
    if query:
        qs = qs.filter(Q(reference__iexact=query) | Q(member__email__iexact=query))
    if flagged:
        qs = qs.filter(high_risk=True)
    return qs
