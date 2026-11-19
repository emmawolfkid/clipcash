"""
Staff-verified manual membership payments with configurable, versioned providers.

A member chooses an active instruction version for a pending plan. An immutable quote
binds those receiving details and the subscription price before payment is submitted.
Edits and retirement never rewrite issued instructions or claims. Staff verify the
original recipient, amount and receipt, then approval posts a balanced ledger entry
and activates the membership in one transaction. A provider's receipt remains single
use across receiving-number and display-label changes.

Legacy Airtel ledger receipts and AIRTEL-LIPA activation references are preserved.
Other providers use a stable code in their ledger and activation receipt namespace.
All existing feature gates, review permissions, member locks and ledger checks apply.
"""

import hashlib
import re
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.phone_numbers import normalize_phone_number
from apps.audit.services import DENIED, FAILURE, record_business_event, record_security_event
from apps.core import features, ratelimit
from apps.core.locking import transaction_lock_mariadb
from apps.core.money import format_amount as format_tzs
from apps.memberships import services as memberships
from apps.memberships.models import MembershipPlan, Subscription
from apps.wallets import services as ledger
from apps.wallets.exceptions import LedgerError

from . import evidence as payment_evidence
from .models import MANUAL_METHOD_AIRTEL_LIPA, MANUAL_OPEN_OR_FINAL
from .models import ManualMembershipPayment as Claim
from .models import ManualPaymentInstruction as Instruction
from .models import ManualPaymentQuote as Quote
from .providers import sandbox_active

C = Claim.Status
SOURCE_TYPE = "manual_membership_payment"
POSTING_TYPE = "membership.manual_payment_received"
REFERENCE_TEXT_RE = re.compile(r"^[A-Z0-9][A-Z0-9 .\-/]*[A-Z0-9]$")
BUSINESS_NUMBER_RE = re.compile(r"^[0-9]{4,15}$")
NOTE_MIN = 5
CLAIM_WINDOW_SECONDS = 3600
PAID_ON_MAX_AGE_DAYS = 30


class ManualPaymentError(Exception):
    code = "manual_payment_error"

    def __init__(self, message, *, code=None):
        super().__init__(message)
        if code:
            self.code = code


class NotAuthorized(ManualPaymentError):
    code = "not_authorized"


class Unavailable(ManualPaymentError):
    code = "payments_disabled"


class InvalidState(ManualPaymentError):
    code = "invalid_state"


class RateLimited(ManualPaymentError):
    code = "rate_limited"


# --- Gates and permissions ---------------------------------------------------------------


def manual_payments_enabled():
    """Environment switch on, not a restricted demo (inside the feature), no sandbox
    payments in this deployment, and memberships enabled."""
    return bool(features.is_enabled("manual_membership_payments") and not sandbox_active()
                and features.is_enabled("memberships"))


def active_instructions():
    return Instruction.objects.filter(is_active=True).order_by("provider_label", "method")


def active_instruction(method=None):
    qs = active_instructions()
    return qs.filter(method=method).first() if method else qs.first()


def receipt_key(method, reference_key):
    """Stable receipt namespace; legacy Airtel ledger keys stay byte-for-byte valid."""
    return reference_key if method == MANUAL_METHOD_AIRTEL_LIPA else f"{method}:{reference_key}"


def activation_reference(method, reference_key):
    if method == MANUAL_METHOD_AIRTEL_LIPA:
        return memberships.MANUAL_REFERENCE_PREFIX + reference_key
    return "MANUAL:" + receipt_key(method, reference_key)


def issue_instructions(member, subscription, instruction):
    if not manual_payments_enabled():
        raise Unavailable("Manual membership payments are not available.")
    eligible, reason = memberships.member_eligibility(member)
    if subscription.member_id != getattr(member, "pk", None) or not eligible:
        raise NotAuthorized("You can only request payment details for your own eligible account.", code=reason)
    with transaction.atomic():
        memberships._lock_member(member)
        subscription = Subscription.objects.select_for_update().get(pk=subscription.pk)
        if subscription.status != Subscription.Status.PENDING or subscription.pending_until <= timezone.now():
            raise InvalidState("Choose an unexpired plan first.", code="not_pending")
        if subscription.plan.status != MembershipPlan.Status.ACTIVE:
            raise InvalidState("This plan is no longer available.", code="plan_unavailable")
        instruction = Instruction.objects.select_for_update().get(pk=instruction.pk)
        if not instruction.is_active:
            raise Unavailable("This payment method is retired.", code="no_instructions")
        quote = Quote.objects.filter(subscription=subscription, instruction=instruction).first()
        if quote is None:
            quote = Quote.objects.create(subscription=subscription, instruction=instruction,
                                         amount=subscription.price_amount, currency=subscription.currency)
            record_business_event("membership.payment_instructions.issued", actor=member, target=quote,
                                  metadata={"method": instruction.method, "instruction": str(instruction.pk),
                                            "subscription": str(subscription.pk), "amount": quote.amount})
        return quote


def manual_payments_ready():
    """New instructions can be issued: enabled AND an active method is published."""
    return manual_payments_enabled() and active_instruction() is not None


# PAY-LIVE-01C: the one purchase state every page, message and purchase endpoint derives from.
PURCHASE_OPEN = "open"                # Manual payments on and a payment method published.
PURCHASE_UNPUBLISHED = "unpublished"  # Manual payments on, but no payment method published yet.
PURCHASE_CLOSED = "closed"            # Manual payments off (live gateways are locked in code).
PURCHASE_SANDBOX = "sandbox"          # Isolated test deployments only; never activates anything.
PURCHASE_CHOOSABLE = frozenset({PURCHASE_OPEN, PURCHASE_SANDBOX})

# The single explanation members see whenever purchases are not open (unpublished or closed).
PURCHASE_CLOSED_MESSAGE = _("Membership payments are not open yet, so plans cannot be bought right now. "
                            "Do not send any money.")


def purchase_state():
    """Whether members can buy a membership in this deployment, and how. Built only from the
    existing gates (manual_payments_enabled, the published instructions and sandbox_active);
    manual payments and the sandbox are mutually exclusive, so exactly one state applies."""
    if manual_payments_enabled():
        return PURCHASE_OPEN if active_instruction() is not None else PURCHASE_UNPUBLISHED
    return PURCHASE_SANDBOX if sandbox_active() else PURCHASE_CLOSED


def _staff(user):
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.is_active
                and user.is_staff and user.status == user.Status.ACTIVE)


def can_verify(user):
    return memberships.is_super_admin(user) or (_staff(user) and user.has_perm("payments.verify_manual_payments"))


def can_view(user):
    return can_verify(user) or (_staff(user) and user.has_perm("payments.view_manual_payments"))


def can_manage_instructions(user):
    """Where members send money: Super Administrators only."""
    return memberships.is_super_admin(user)


def _refuse(action, actor, error, target=None):
    record_security_event(
        "membership.manual_payment.refused", outcome=DENIED if isinstance(error, NotAuthorized) else FAILURE,
        actor=actor if getattr(actor, "is_authenticated", False) else None, target=target,
        metadata={"action": action, "reason": getattr(error, "code", "invalid")},
    )


# --- Input normalization -----------------------------------------------------------------


def normalize_reference(raw, field="transaction_reference"):
    """(display text, single-use key). Upper case; letters, digits and . - / and single
    spaces only; the key keeps letters and digits, so "ab.12-34" and "AB1234" collide."""
    text = " ".join(str(raw or "").split()).upper()
    error = ValidationError({field: _("Enter the transaction reference exactly as it appears in the provider "
                                      "confirmation message (letters and digits, 6 to 40 characters).")})
    if not text or len(text) > 40 or not REFERENCE_TEXT_RE.match(text):
        raise error
    key = re.sub(r"[^A-Z0-9]", "", text)
    if not 6 <= len(key) <= 40 or len(set(key)) == 1:
        raise error
    return text, key


def normalize_amount(raw, field="amount"):
    text = str(raw if raw is not None else "").strip().replace(",", "").replace(" ", "")
    if text.upper().startswith("TZS"):
        text = text[3:]
    if not text.isdigit() or len(text) > 12:
        raise ValidationError({field: _("Enter the amount in whole shillings, for example 5000.")})
    return int(text)


def _normalize_phone(raw):
    try:
        phone = normalize_phone_number(raw)
    except ValidationError as error:
        raise ValidationError({"payer_phone": error.messages}) from None
    if not phone:
        raise ValidationError({"payer_phone": _("Enter the phone number you paid from.")})
    return phone


def _check_paid_on(paid_on, subscription):
    if paid_on is None:
        return None
    today = timezone.localdate()
    if paid_on > today + timedelta(days=1):
        raise ValidationError({"paid_on": _("The payment date cannot be in the future.")})
    earliest = timezone.localdate(subscription.created_at) - timedelta(days=1)
    if paid_on < earliest or paid_on < today - timedelta(days=PAID_ON_MAX_AGE_DAYS):
        raise ValidationError({"paid_on": _("The payment date must be after you chose this plan.")})
    return paid_on


def _rate_key(member):
    return hashlib.sha256(f"manual-membership-claim:{member.pk}".encode()).hexdigest()


# --- Member: submit and withdraw claims ------------------------------------------------------


def payable_subscription(member):
    """The member's open choice that a manual payment can be submitted for, if any."""
    return (Subscription.objects.select_related("plan")
            .filter(member=member, status=Subscription.Status.PENDING, pending_until__gt=timezone.now())
            .first())


def open_claim(subscription):
    return subscription.manual_payments.filter(status=C.PENDING_VERIFICATION).first()


def submit_claim(member, subscription, *, amount, payer_phone, transaction_reference, paid_on=None, quote=None,
                 evidence=None):
    """Record the member's statement that they paid. Returns (claim, created).

    PRD-04: ``evidence`` is an optional uploaded receipt (JPEG, PNG or PDF, checked by content and
    size by apps.payments.evidence). It is stored privately and attached to the NEW claim only;
    it is never proof of payment.

    Repeating the same submission (double click, retried request) returns the open
    claim unchanged (any new evidence is ignored). Raises ValidationError (field errors for the
    form), NotAuthorized, Unavailable, InvalidState or RateLimited."""
    try:
        if not manual_payments_enabled():
            raise Unavailable("Manual membership payments are not available.")
        if subscription.member_id != getattr(member, "pk", None):
            raise NotAuthorized("You can only pay for your own membership.")
        eligible, reason = memberships.member_eligibility(member)  # No demo waiver: real money.
        if not eligible:
            raise NotAuthorized("Your account cannot make payments yet.", code=reason)
        if ratelimit.hit(_rate_key(member), CLAIM_WINDOW_SECONDS) > settings.MANUAL_PAYMENTS_CLAIMS_PER_HOUR:
            raise RateLimited("Too many payment submissions. Please wait an hour and try again.")
        amount = normalize_amount(amount)
        payer_phone = _normalize_phone(payer_phone)
        reference_text, reference_key = normalize_reference(transaction_reference)
        evidence_info = payment_evidence.inspect(evidence) if evidence else None  # Before any lock is taken.
        now = timezone.now()
        with transaction.atomic():
            member = memberships._lock_member(member)  # Same lock order as every membership change.
            subscription = Subscription.objects.select_for_update().select_related("plan").get(pk=subscription.pk)
            if quote is not None:
                try:
                    quote = Quote.objects.select_related("instruction").get(
                        pk=getattr(quote, "pk", quote), subscription=subscription)
                except (Quote.DoesNotExist, ValueError, TypeError):
                    raise ValidationError({"quote": _("Choose payment instructions for this membership.")}) from None
            existing = open_claim(subscription)
            if existing is not None:
                if (existing.reference_key == reference_key and existing.amount == amount
                        and (quote is None or existing.instruction_id == quote.instruction_id)):
                    return existing, False
                raise InvalidState("A payment for this membership is already waiting for verification.",
                                   code="already_submitted")
            if subscription.status != Subscription.Status.PENDING:
                raise InvalidState("This membership is not waiting for payment.", code="not_pending")
            if subscription.pending_until <= now:
                raise InvalidState("This membership choice has expired. Choose the plan again.",
                                   code="choice_expired")
            if subscription.plan.status != MembershipPlan.Status.ACTIVE:
                raise InvalidState("This plan is no longer available.", code="plan_unavailable")
            if quote is None and active_instructions().count() > 1:
                raise ValidationError({"quote": _("Choose a payment method and get its instructions first.")})
            instruction = quote.instruction if quote else active_instruction()
            if instruction is None:
                raise Unavailable("Payment details are not published.", code="no_instructions")
            if quote is None:
                quote = issue_instructions(member, subscription, instruction)
            if amount != quote.amount:
                raise ValidationError({"amount": _("The amount must be exactly %(price)s, the price of your "
                                                   "plan.") % {"price": format_tzs(subscription.price_amount)}})
            _check_paid_on(paid_on, subscription)
            if (Claim.objects.filter(method=instruction.method, reference_key=reference_key, status__in=MANUAL_OPEN_OR_FINAL).exists()
                    or Subscription.objects.filter(
                        payment_reference=activation_reference(instruction.method, reference_key)).exists()):
                raise ValidationError({"transaction_reference": ValidationError(_(
                    "This transaction reference has already been submitted. Each provider payment can be used "
                    "for one membership only. Contact support if you think this is a mistake."),
                    code="duplicate_reference")})
            resubmission = subscription.manual_payments.filter(status=C.REJECTED).exists()
            try:
                with transaction.atomic():
                    claim = Claim.objects.create(
                        member=member, subscription=subscription, instruction=instruction, method=instruction.method, quote=quote,
                        plan_name=subscription.plan_name, amount=amount, currency=subscription.currency,
                        payer_phone=payer_phone, transaction_reference=reference_text, reference_key=reference_key,
                        paid_on=paid_on)
            except IntegrityError:
                # A concurrent claim took the same reference (or this membership) first.
                raise ValidationError({"transaction_reference": ValidationError(_(
                    "This transaction reference has already been submitted."), code="duplicate_reference")}) from None
            record_business_event("membership.manual_payment.submitted", actor=member, target=claim,
                                  metadata={"reference": claim.reference, "amount": amount,
                                            "plan_code": subscription.plan.code, "resubmission": resubmission})
            _notify(claim, "submitted", _("Payment submitted for verification"),
                    _("We received your payment details for %(plan)s (%(amount)s, reference %(ref)s). Your "
                      "membership starts after our team confirms the payment in the provider business records.")
                    % {"plan": claim.plan_name, "amount": format_tzs(amount), "ref": claim.transaction_reference},
                    email=True)
            if evidence_info is not None:
                _attach_evidence(claim, member, evidence, *evidence_info)
            return claim, True
    except ValidationError as error:
        codes = {e.code for e in error.error_list} if not hasattr(error, "error_dict") else {
            e.code for errors in error.error_dict.values() for e in errors}
        if "duplicate_reference" in codes:
            _refuse("submit", member, ManualPaymentError("", code="duplicate_reference"), subscription)
        raise
    except ManualPaymentError as error:
        _refuse("submit", member, error, subscription)
        raise


def _attach_evidence(claim, member, upload, content_type, size, sha256):
    """Store the file privately, then record it, inside the claim's transaction (the last step,
    so nothing after it can roll the row back and orphan the file). If recording fails, the
    stored file is removed and the whole submission rolls back."""
    from .models import ManualPaymentEvidence

    stored = payment_evidence.save(claim, upload, content_type)
    try:
        with transaction.atomic():
            ManualPaymentEvidence.objects.create(claim=claim, uploaded_by=member, storage_name=stored,
                                                 content_type=content_type, size=size, sha256=sha256)
    except BaseException:
        payment_evidence.delete_quietly(stored)
        raise


def can_download_evidence(user, claim):
    """PRD-04: the claim's own member, or staff who may view manual payments."""
    if not getattr(user, "is_authenticated", False):
        return False
    return claim.member_id == user.pk or can_view(user)


def evidence_duplicates(evidence):
    """Other claims with byte-identical evidence (a reused screenshot is a fraud signal for the
    reviewer; it is not refused, because evidence is never proof either way)."""
    from .models import ManualPaymentEvidence

    return (ManualPaymentEvidence.objects.filter(sha256=evidence.sha256).exclude(pk=evidence.pk)
            .select_related("claim"))


def cancel_claim(member, claim):
    """The member withdraws their own claim while it is still pending."""
    try:
        if claim.member_id != getattr(member, "pk", None):
            raise NotAuthorized("You can only withdraw your own payment details.")
        with transaction.atomic():
            memberships._lock_member(member)
            claim = Claim.objects.select_for_update().get(pk=claim.pk)
            if claim.status != C.PENDING_VERIFICATION:
                raise InvalidState("Only payments waiting for verification can be withdrawn.", code="not_pending")
            claim.status, claim.cancelled_at = C.CANCELLED, timezone.now()
            claim.save(update_fields=["status", "cancelled_at", "updated_at"])
            record_business_event("membership.manual_payment.cancelled", actor=member, target=claim,
                                  metadata={"reference": claim.reference})
            return claim
    except ManualPaymentError as error:
        _refuse("cancel", member, error, claim)
        raise


# --- Staff: approve or reject ---------------------------------------------------------------


def _lock_claim(claim_id):
    """Lock in the fixed order member -> claim -> subscription (as membership services do)."""
    member_id = Claim.objects.values_list("member_id", flat=True).get(pk=claim_id)
    get_user_model().objects.select_for_update().get(pk=member_id)
    return Claim.objects.select_for_update().get(pk=claim_id)


def approve(actor, claim, *, verified_amount, verified_reference, receipt_confirmed, recipient_confirmed=None,
            note="", verified_receiving_number=None, verified_recipient_name=None):
    """Approve after independently confirming the payment in the official provider business
    records: the reviewer re-enters the amount and reference the provider shows, confirms the
    transaction is in those records (receipt_confirmed) and that it was received into the
    receiving account the member was shown (recipient_confirmed). The member's own statement
    is never enough. Atomic and idempotent: approving an approved claim again changes
    nothing and returns (claim, False). Returns (claim, True) when this call approved it."""
    try:
        if not manual_payments_enabled():
            raise Unavailable("Manual membership payments are not enabled.")
        if not can_verify(actor):
            raise NotAuthorized("You are not allowed to verify membership payments.")
        errors = {}
        if receipt_confirmed is not True:
            errors["receipt_confirmed"] = _("Confirm that you found this transaction in the official provider "
                                            "business records.")
        if recipient_confirmed is not True:
            errors["recipient_confirmed"] = _("Confirm that the payment was received into the receiving account "
                                              "shown to the member.")
        if errors:
            raise ValidationError(errors)
        verified_amount = normalize_amount(verified_amount, field="verified_amount")
        _text, verified_key = normalize_reference(verified_reference, field="verified_reference")
        note = " ".join(str(note or "").split())[:300]
        now = timezone.now()
        with transaction.atomic():
            claim = _lock_claim(claim.pk)
            if claim.status == C.APPROVED:
                return claim, False  # Repeated approval: nothing changes.
            if claim.status != C.PENDING_VERIFICATION:
                raise InvalidState(f"This payment is already {claim.get_status_display().lower()}.",
                                   code="not_pending")
            if claim.member_id == actor.pk:
                raise NotAuthorized("You cannot verify your own payment.", code="own_payment")
            instruction = claim.instruction
            if verified_receiving_number is not None or claim.method != MANUAL_METHOD_AIRTEL_LIPA:
                number = re.sub(r"\s", "", str(verified_receiving_number or ""))
                if number != instruction.business_number:
                    raise ValidationError({"verified_receiving_number": _("The receiving number does not match the issued instructions.")})
            if verified_recipient_name is not None or claim.method != MANUAL_METHOD_AIRTEL_LIPA:
                name = " ".join(str(verified_recipient_name or "").split()).casefold()
                if name != " ".join(instruction.recipient_name.split()).casefold():
                    raise ValidationError({"verified_recipient_name": _("The registered recipient does not match the issued instructions.")})

            if verified_amount != claim.amount:
                raise ValidationError({"verified_amount": _(
                    "The verified amount differs from the plan price (%(price)s). Reject the payment with "
                    "the reason “amount does not match” instead.") % {"price": format_tzs(claim.amount)}})
            if verified_key != claim.reference_key:
                raise ValidationError({"verified_reference": _(
                    "The reference you found differs from the one the member submitted. Check it again, or "
                    "reject the payment.")})
            subscription = (Subscription.objects.select_for_update().select_related("plan", "member")
                            .get(pk=claim.subscription_id))
            entry = ledger.post_entry(
                POSTING_TYPE,
                lines=[ledger.debit(ledger.get_platform_account("CASH"), claim.amount, f"{claim.instruction.provider_label} receipt"),
                       ledger.credit(ledger.get_platform_account("MEMBERSHIP_DEFERRED_REVENUE"), claim.amount,
                                     f"Membership {claim.plan_name}"[:200])],
                idempotency_key=f"manual-membership:{claim.pk}", source_type=SOURCE_TYPE, source_id=str(claim.pk),
                external_reference=receipt_key(claim.method, claim.reference_key),
                description=f"Membership fee {claim.reference}, verified by staff ({claim.plan_name})",
                actor=None,  # Posted by this verification process; the reviewer is recorded on the claim.
            )
            # The shared activation rule (apps.memberships.services._activate), as a future
            # verified provider payment would use it: the exact plan chosen, once.
            memberships.activate_with_manual_payment(subscription, reference_key=claim.reference_key,
                                                     amount=claim.amount, actor=actor,
                                                     submitted_at=claim.created_at, now=now, method=claim.method)
            subscription.refresh_from_db()
            claim.status, claim.reviewed_by, claim.reviewed_at = C.APPROVED, actor, now
            claim.verified_amount, claim.verified_reference_key = verified_amount, verified_key
            claim.receipt_confirmed, claim.review_note, claim.ledger_entry_id = True, note, entry.pk
            claim.save(update_fields=["status", "reviewed_by", "reviewed_at", "verified_amount",
                                      "verified_reference_key", "receipt_confirmed", "review_note",
                                      "ledger_entry_id", "updated_at"])
            record_business_event("membership.manual_payment.approved", actor=actor, target=claim,
                                  metadata={"reference": claim.reference, "amount": claim.amount,
                                            "plan_code": subscription.plan.code, "ledger_entry": str(entry.pk),
                                            "subscription": str(subscription.pk),
                                            "transaction_reference_key": claim.reference_key,
                                            "recipient": claim.instruction.business_number,
                                            "expires_at": subscription.expires_at.isoformat()})
            _notify(claim, "approved", _("Payment confirmed: membership active"),
                    _("Your payment of %(amount)s (reference %(ref)s) was confirmed. Your %(plan)s membership is "
                      "active until %(expires)s.")
                    % {"amount": format_tzs(claim.amount), "ref": claim.transaction_reference,
                       "plan": claim.plan_name,
                       "expires": timezone.localtime(subscription.expires_at).strftime("%d %b %Y")}, email=True)
            return claim, True
    except IntegrityError as error:
        conflict = InvalidState("The approval conflicted with another record. Nothing was changed.",
                                code="conflict")
        _refuse("approve", actor, conflict, claim)
        raise conflict from error
    except LedgerError as error:
        failure = InvalidState("The ledger refused the posting. Nothing was changed.", code=error.code)
        _refuse("approve", actor, failure, claim)
        raise failure from error
    except memberships.MembershipError as error:
        failure = InvalidState(f"The membership could not be activated: {error}", code=error.code)
        _refuse("approve", actor, failure, claim)
        raise failure from error
    except ManualPaymentError as error:
        _refuse("approve", actor, error, claim)
        raise


def reject(actor, claim, *, reason, note=""):
    """Reject with a reason the member sees. The member may submit corrected details."""
    try:
        if not manual_payments_enabled():
            raise Unavailable("Manual membership payments are not enabled.")
        if not can_verify(actor):
            raise NotAuthorized("You are not allowed to verify membership payments.")
        if reason not in Claim.RejectionReason.values:
            raise ValidationError({"reason": _("Choose a reason.")})
        note = " ".join(str(note or "").split())[:300]
        if reason == Claim.RejectionReason.OTHER and len(note) < NOTE_MIN:
            raise ValidationError({"note": _("Explain the reason in a few words.")})
        with transaction.atomic():
            claim = _lock_claim(claim.pk)
            if claim.status == C.REJECTED:
                return claim, False
            if claim.status != C.PENDING_VERIFICATION:
                raise InvalidState(f"This payment is already {claim.get_status_display().lower()}.",
                                   code="not_pending")
            if claim.member_id == actor.pk:
                raise NotAuthorized("You cannot verify your own payment.", code="own_payment")
            claim.status, claim.reviewed_by, claim.reviewed_at = C.REJECTED, actor, timezone.now()
            claim.rejection_reason, claim.review_note = reason, note
            claim.save(update_fields=["status", "reviewed_by", "reviewed_at", "rejection_reason", "review_note",
                                      "updated_at"])
            record_business_event("membership.manual_payment.rejected", actor=actor, target=claim,
                                  metadata={"reference": claim.reference, "reason": reason})
            message = _("We could not confirm your payment for %(plan)s (reference %(ref)s): %(reason)s.") % {
                "plan": claim.plan_name, "ref": claim.transaction_reference,
                "reason": str(claim.get_rejection_reason_display()).lower()}
            if note:
                message = f"{message} {note}"
            _notify(claim, "rejected", _("Payment not confirmed"),
                    str(message) + " " + str(_("You can submit corrected details or contact support.")),
                    email=True)
            return claim, True
    except ManualPaymentError as error:
        _refuse("reject", actor, error, claim)
        raise


def _notify(claim, event, title, message, *, email):
    from apps.notifications import inbox

    inbox.notify(claim.member, f"membership.payment_{event}", category=inbox.Category.MEMBERSHIP,
                 title=str(title), message=str(message)[:inbox.MAX_MESSAGE],
                 link=inbox.link_to("payments:manual_claim", claim.pk),
                 dedupe_key=f"membership.payment_{event}:{claim.pk}", email=email)


# --- Super Administrators: payment instructions -----------------------------------------------


def publish_instruction(actor, *, business_number, recipient_name, instructions="",
                        method=MANUAL_METHOD_AIRTEL_LIPA, provider_label=None):
    """Publish a provider revision, retaining its stable receipt namespace and all prior details."""
    try:
        if not can_manage_instructions(actor):
            raise NotAuthorized("Only Super Administrators manage payment details.")
        business_number = re.sub(r"\s", "", str(business_number or ""))
        recipient_name = " ".join(str(recipient_name or "").split())
        instructions = str(instructions or "").strip()
        method = str(method).strip().upper()
        provider_label = " ".join(str(provider_label or ("Airtel Money - Lipa" if method == MANUAL_METHOD_AIRTEL_LIPA else method)).split())
        errors = {}
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,19}", method):
            errors["method"] = _("Use 2 to 20 uppercase letters, digits or underscores, starting with a letter.")
        if not 2 <= len(provider_label) <= 100:
            errors["provider_label"] = _("Enter the provider name shown to customers (2 to 100 characters).")
        if not BUSINESS_NUMBER_RE.match(business_number):
            errors["business_number"] = _("Enter the receiving number: digits only, 4 to 15 of them.")
        if not 2 <= len(recipient_name) <= 100:
            errors["recipient_name"] = _("Enter the recipient name shown by the provider when paying.")
        if len(instructions) > 1000:
            errors["instructions"] = _("Keep the instructions to 1000 characters or fewer.")
        if errors:
            raise ValidationError(errors)
        with transaction.atomic():
            _lock_instructions()
            replaced = _retire_active(actor, method)
            instruction = Instruction.objects.create(
                method=method, provider_label=provider_label, business_number=business_number, recipient_name=recipient_name,
                instructions=instructions, is_active=True, created_by=actor)
            record_business_event("membership.payment_instructions.published", actor=actor, target=instruction,
                                  metadata={"method": method, "provider_label": provider_label, "business_number": business_number,
                                            "recipient_name": recipient_name, "replaced": replaced})
            return instruction
    except ManualPaymentError as error:
        _refuse("instructions.publish", actor, error)
        raise


def retire_instruction(actor, method=MANUAL_METHOD_AIRTEL_LIPA):
    """Stop issuing new quotes for this provider. Issued quotes and pending claims remain valid."""
    try:
        if not can_manage_instructions(actor):
            raise NotAuthorized("Only Super Administrators manage payment details.")
        with transaction.atomic():
            _lock_instructions()
            retired = _retire_active(actor, method)
            if retired:
                record_business_event("membership.payment_instructions.retired", actor=actor, target=None,
                                      metadata={"method": method})
            return retired
    except ManualPaymentError as error:
        _refuse("instructions.retire", actor, error)
        raise


def _lock_instructions():
    if connection.vendor == "mysql":
        transaction_lock_mariadb("manual.payment.instructions")
    else:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                           ["manual.payment.instructions"])


def enable_instruction(actor, instruction):
    if not can_manage_instructions(actor):
        raise NotAuthorized("Only Super Administrators manage payment details.")
    with transaction.atomic():
        _lock_instructions()
        current = active_instruction(instruction.method)
        if current:
            return current
        enabled = publish_instruction(actor, method=instruction.method, provider_label=instruction.provider_label,
                                   business_number=instruction.business_number, recipient_name=instruction.recipient_name,
                                   instructions=instruction.instructions)
        record_business_event("membership.payment_instructions.enabled", actor=actor, target=enabled,
                              metadata={"method": instruction.method, "previous": str(instruction.pk)})
        return enabled


def _retire_active(actor, method):
    current = (Instruction.objects.select_for_update()
               .filter(method=method, is_active=True).first())
    if current is None:
        return False
    current.is_active, current.retired_at, current.retired_by = False, timezone.now(), actor
    current.save(update_fields=["is_active", "retired_at", "retired_by", "updated_at"])
    return True


# --- Read helpers --------------------------------------------------------------------------


def member_claims(member):
    return Claim.objects.filter(member=member).select_related("subscription").order_by("-created_at")


def queue(status=C.PENDING_VERIFICATION):
    qs = Claim.objects.select_related("member", "subscription", "reviewed_by")
    if status:
        qs = qs.filter(status=status)
    # Oldest first while waiting; newest first for decided claims.
    return qs.order_by("created_at" if status == C.PENDING_VERIFICATION else "-created_at")
