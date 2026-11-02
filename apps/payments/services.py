"""
Payment services (Phase 08), SANDBOX FIRST: intents, provider notices,
reconciliation, settlement, isolated sandbox journal, advertiser restricted
funds, allocations, refunds and disputes.

Invariants
----------
* A provider callback is only a notice. Money is recognised only after the
  notice is authenticated, the provider's authoritative status is fetched,
  every field is reconciled against the intent (reference, payer subject,
  purpose, amount, currency, environment), and settlement is confirmed.
* Anything unexpected goes to the ReconciliationException queue. No action in
  this module creates reconciled funds without a matching provider record.
* Membership money and advertiser money are separate funds domains and never
  share a posting; advertiser funds come only from that advertiser's settled
  deposits and fund only that advertiser's campaigns.
* Everything is SANDBOX: postings go to SandboxPosting, never to apps.wallets.
  Campaign.funding_status stays UNFUNDED; memberships are previewed, not
  activated; demo rewards stay non-payable.
* Provider calls never run while a database lock is held.
"""

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.advertising.models import AdvertiserMembership, Campaign, Organization
from apps.audit.services import DENIED, record_business_event, record_security_event
from apps.core import features, ratelimit
from apps.core.money import to_amount
from apps.memberships.models import Subscription
from apps.wallets.posting_types import POSTING_TYPES

from .models import (
    SANDBOX,
    FundsDomain,
    MembershipActivationPreview,
    PaymentDispute,
    PaymentIntent,
    ProviderEvent,
    ReconciliationException,
    RefundRequest,
    SandboxAdvertiserFunds,
    SandboxCampaignAllocation,
    SandboxPosting,
)
from .providers import CallbackRejected, PaymentsDisabled, get_provider, sandbox_active

S = PaymentIntent.Status
Purpose = PaymentIntent.Purpose
Exc = ReconciliationException.Reason
MIN_REASON = 5
ALLOCATABLE_CAMPAIGN_STATES = (Campaign.Status.SCHEDULED, Campaign.Status.ACTIVE, Campaign.Status.PAUSED)


class PaymentError(Exception):
    code = "payment_error"

    def __init__(self, message, *, code=None):
        super().__init__(message)
        if code:
            self.code = code


class NotAuthorized(PaymentError):
    code = "not_authorized"


class NotFound(PaymentError):
    code = "not_found"


class InvalidState(PaymentError):
    code = "invalid_state"


# --- Gates and permissions ------------------------------------------------------------------


def require_sandbox():
    if not sandbox_active():
        raise PaymentsDisabled("Payments are not available in this environment.")


def _staff_with(user, perm):
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.is_active
                and user.is_staff and user.has_perm(perm))


def can_view_payments(user):
    return _staff_with(user, "payments.view_payments")


def can_operate_sandbox(user):
    return _staff_with(user, "payments.operate_sandbox")


def can_review_reconciliation(user):
    return _staff_with(user, "payments.review_reconciliation")


def can_approve_refunds(user):
    return _staff_with(user, "payments.approve_refunds")


def _deny(action, actor, target=None, reason="permission_missing"):
    record_security_event("payments.action.denied", outcome=DENIED,
                          actor=actor if getattr(actor, "is_authenticated", False) else None,
                          target=target, metadata={"action": action, "reason": reason})
    raise NotAuthorized("You are not allowed to do this.", code=reason)


def _require(check, actor, action, target=None):
    if not check(actor):
        _deny(action, actor, target)


def _reason(text):
    text = (text or "").strip()
    if len(text) < MIN_REASON:
        raise ValidationError({"reason": "Please give a reason (at least 5 characters)."})
    return text[:300]


def _account_ok(user):
    from apps.memberships.services import member_eligibility

    return member_eligibility(user)[0]


def _owner_of(user, organization):
    return AdvertiserMembership.objects.filter(organization=organization, user=user, is_active=True,
                                               role=AdvertiserMembership.Role.OWNER).exists()


def _member_of(user, organization):
    return AdvertiserMembership.objects.filter(organization=organization, user=user, is_active=True).exists()


def intent_for(user, intent_id):
    """An intent the user may see: their own membership payment, or a deposit of an
    advertiser they belong to. Anything else is "not found"."""
    intent = (PaymentIntent.objects.select_related("organization", "subscription")
              .filter(pk=intent_id).first())
    if intent is None or not getattr(user, "is_authenticated", False):
        raise NotFound("Payment not found.")
    if intent.purpose == Purpose.MEMBERSHIP and intent.payer_id == user.pk:
        return intent
    if intent.purpose == Purpose.ADVERTISER_DEPOSIT and not user.is_staff and _member_of(user, intent.organization):
        return intent
    raise NotFound("Payment not found.")


# --- Sandbox journal ------------------------------------------------------------------------


def _post(posting_type, *, debit, credit, amount, intent, key, domain, debit_scope="", credit_scope="", memo=""):
    """One balanced sandbox posting under a real-chart posting type. Idempotent by key."""
    rule = POSTING_TYPES[posting_type]
    if debit not in rule.debit_types or credit not in rule.credit_types:
        raise InvalidState(f"{posting_type} cannot debit {debit} / credit {credit}.")
    existing = SandboxPosting.objects.filter(idempotency_key=key).first()
    if existing is not None:
        return existing
    posting = SandboxPosting.objects.create(
        posting_type=posting_type, debit_account=debit, debit_scope=debit_scope, credit_account=credit,
        credit_scope=credit_scope, amount=amount, funds_domain=domain, intent=intent, idempotency_key=key,
        memo=memo[:200])
    record_business_event("payments.sandbox.posted", actor=None, target=intent, metadata={
        "posting_type": posting_type, "amount": amount, "funds_domain": domain})
    return posting


def sandbox_balances():
    """{(account, scope): normal-side-agnostic debit balance} from the sandbox journal."""
    balances = {}
    for row in SandboxPosting.objects.values("debit_account", "debit_scope").annotate(total=Sum("amount")):
        key = (row["debit_account"], row["debit_scope"])
        balances[key] = balances.get(key, 0) + row["total"]
    for row in SandboxPosting.objects.values("credit_account", "credit_scope").annotate(total=Sum("amount")):
        key = (row["credit_account"], row["credit_scope"])
        balances[key] = balances.get(key, 0) - row["total"]
    return dict(sorted(balances.items()))


# --- Intents ---------------------------------------------------------------------------------


def _idempotency_key(value):
    value = str(value or "").strip()
    if not 8 <= len(value) <= 64:
        raise ValidationError({"idempotency_key": "Missing form token. Reload the page and try again."})
    return value


def _new_intent(*, payer, purpose, amount, idempotency_key, **subject):
    existing = PaymentIntent.objects.filter(payer=payer, idempotency_key=idempotency_key).first()
    if existing is not None:
        if existing.purpose != purpose or existing.amount != amount:
            raise ValidationError("This form was already used for a different payment.")
        return existing, False
    domain = FundsDomain.MEMBERSHIP_REVENUE if purpose == Purpose.MEMBERSHIP else \
        FundsDomain.ADVERTISER_RESTRICTED_FUNDS
    provider = get_provider()
    try:
        with transaction.atomic():
            intent = PaymentIntent.objects.create(
                payer=payer, purpose=purpose, funds_domain=domain, amount=amount, provider=provider.code,
                idempotency_key=idempotency_key,
                expires_at=timezone.now() + timedelta(minutes=settings.PAYMENTS_INTENT_MINUTES), **subject)
            record_business_event("payments.intent.created", actor=payer, target=intent, metadata={
                "purpose": purpose, "amount": amount, "environment": SANDBOX})
    except IntegrityError:
        existing = PaymentIntent.objects.filter(payer=payer, idempotency_key=idempotency_key).first()
        if existing is None:
            raise  # e.g. another open payment for the same membership.
        return existing, False  # Double submit raced: same intent.
    # The provider request happens outside the creating transaction (no lock held).
    request = provider.create_payment(intent)
    with transaction.atomic():
        intent = PaymentIntent.objects.select_for_update().get(pk=intent.pk)
        if intent.status == S.CREATED:
            intent.provider_reference = request.provider_reference
            intent.status = S.PENDING
            intent.save()
    return intent, True


def create_membership_intent(member, subscription, *, idempotency_key):
    """Sandbox payment for a PENDING subscription. The amount is the quoted price;
    the browser never supplies an amount for memberships."""
    require_sandbox()
    if not features.is_enabled("memberships"):
        raise InvalidState("Memberships are not available.", code="feature_disabled")
    if not _account_ok(member):
        _deny("intent.membership", member, subscription, "account_not_eligible")
    key = _idempotency_key(idempotency_key)
    subscription = Subscription.objects.filter(pk=subscription.pk).first()
    if subscription is None or subscription.member_id != member.pk:
        raise NotFound("Membership not found.")
    if subscription.status != Subscription.Status.PENDING or subscription.pending_until <= timezone.now():
        raise InvalidState("This membership is not awaiting payment.", code="not_pending")
    if subscription.currency != "TZS":
        raise ValidationError({"currency": "Unsupported currency."})
    try:
        return _new_intent(payer=member, purpose=Purpose.MEMBERSHIP, amount=subscription.price_amount,
                           idempotency_key=key, subscription=subscription)[0]
    except IntegrityError:
        raise InvalidState("A payment for this membership is already in progress.", code="open_intent") from None


def create_deposit_intent(user, organization, *, amount, idempotency_key):
    """Sandbox deposit into an advertiser's restricted funds. Owners only."""
    require_sandbox()
    if not features.is_enabled("advertisers"):
        raise InvalidState("Advertiser features are not available.", code="feature_disabled")
    if not _account_ok(user) or user.is_staff or not _owner_of(user, organization):
        _deny("intent.deposit", user, organization, "owner_required")
    if organization.verification_status != Organization.Verification.APPROVED:
        raise InvalidState("Only approved advertisers can make deposits.", code="organization_not_approved")
    key = _idempotency_key(idempotency_key)
    try:
        amount = to_amount(amount)
    except (ValueError, TypeError):
        raise ValidationError({"amount": "Enter a whole number of shillings."}) from None
    if not settings.PAYMENTS_DEPOSIT_MIN <= amount <= settings.PAYMENTS_DEPOSIT_MAX:
        raise ValidationError({"amount": f"Deposits must be between TZS {settings.PAYMENTS_DEPOSIT_MIN:,} "
                                         f"and TZS {settings.PAYMENTS_DEPOSIT_MAX:,}."})
    return _new_intent(payer=user, purpose=Purpose.ADVERTISER_DEPOSIT, amount=amount, idempotency_key=key,
                       organization=organization)[0]


def cancel_intent(user, intent):
    with transaction.atomic():
        intent = PaymentIntent.objects.select_for_update().get(pk=intent.pk)
        if intent.payer_id != user.pk:
            _deny("intent.cancel", user, intent, "not_payer")
        if intent.status in (S.CANCELLED,):
            return intent
        if intent.status not in (S.CREATED, S.PENDING):
            raise InvalidState("Only payments that have not been confirmed can be cancelled.")
        _close(intent, S.CANCELLED, "payer_cancelled")
        record_business_event("payments.intent.cancelled", actor=user, target=intent,
                              metadata={"reference": intent.reference})
    return intent


def _close(intent, status, reason):
    intent.status = status
    intent.status_reason = reason[:100]
    intent.closed_at = timezone.now()
    intent.save()


# --- Provider notices --------------------------------------------------------------------------


def _throttle(provider_code):
    import hashlib

    key = hashlib.sha256(f"payments-callbacks:{provider_code}".encode()).hexdigest()
    return ratelimit.hit(key, 60) > settings.PAYMENTS_CALLBACKS_PER_MINUTE


def receive_callback(provider_code, body, signature):
    """Authenticated provider notice. Returns (http_status, outcome). Never trusts body amounts."""
    require_sandbox()
    provider = get_provider(provider_code)
    if not isinstance(body, bytes) or len(body) > settings.PAYMENTS_CALLBACK_MAX_BYTES:
        return 413, "too_large"
    if _throttle(provider.code):
        return 429, "rate_limited"
    try:
        notice = provider.validate_callback(body, signature)
    except CallbackRejected as error:
        ProviderEvent.objects.create(provider=provider.code, event_id="unauthenticated", kind=ProviderEvent.Kind.CALLBACK,
                                     authenticated=False, outcome=ProviderEvent.Outcome.REFUSED, reason=error.reason)
        record_security_event("payments.callback.refused", outcome=DENIED, actor=None,
                              metadata={"provider": provider.code, "reason": error.reason})
        return 401, error.reason
    outcome = process_provider_update(provider, notice.event_id, notice.provider_reference,
                                      kind=ProviderEvent.Kind.CALLBACK)
    return 200, outcome


def process_provider_update(provider, event_id, provider_reference, *, kind):
    """Fetch the authoritative status (no lock held), then apply it exactly once."""
    status = provider.fetch_status(provider_reference)  # Provider call outside any transaction.
    summary = ({"status": status.status, "amount": status.amount, "currency": status.currency,
                "settled": status.settled} if status else {})
    duplicate = False
    with transaction.atomic():
        try:
            with transaction.atomic():
                event = ProviderEvent.objects.create(provider=provider.code, event_id=event_id, kind=kind,
                                                     authenticated=True, outcome=ProviderEvent.Outcome.NO_CHANGE,
                                                     provider_reference=provider_reference, summary=summary)
        except IntegrityError:
            duplicate = True
        if duplicate:
            ProviderEvent.objects.create(provider=provider.code, event_id=event_id[:100], kind=kind,
                                         authenticated=False, outcome=ProviderEvent.Outcome.DUPLICATE,
                                         reason="replayed_event", provider_reference=provider_reference)
        else:
            outcome, reason, intent = _apply(provider, status, provider_reference)
            ProviderEvent.objects.filter(pk=event.pk).update(outcome=outcome, reason=reason, intent=intent)
            record_business_event("payments.callback.accepted", actor=None, target=intent, metadata={
                "provider": provider.code, "kind": kind, "status": status.status if status else "unknown"})
    if duplicate:
        record_security_event("payments.callback.duplicate", outcome=DENIED, actor=None,
                              metadata={"provider": provider.code, "kind": kind})
        return ProviderEvent.Outcome.DUPLICATE
    return outcome


def _exception(reason, *, intent=None, organization=None, provider_reference="", detail="", amount=None):
    open_same = ReconciliationException.objects.filter(intent=intent, organization=organization, reason=reason,
                                                       provider_reference=provider_reference,
                                                       status=ReconciliationException.Status.OPEN)
    exc = open_same.first()
    if exc is None:
        exc = ReconciliationException.objects.create(intent=intent, organization=organization, reason=reason,
                                                     provider_reference=provider_reference, detail=detail[:300],
                                                     amount=amount)
        record_business_event("payments.exception.opened", actor=None, target=intent or organization,
                              metadata={"reason": reason, "amount": amount or 0})
    return exc


def _apply(provider, status, provider_reference):
    """(outcome, reason, intent). Runs inside the event transaction."""
    E = ProviderEvent.Outcome
    intent = (PaymentIntent.objects.select_for_update()
              .filter(provider=provider.code, provider_reference=provider_reference).first())
    if status is None or intent is None:
        _exception(Exc.UNKNOWN_REFERENCE, provider_reference=provider_reference,
                   detail="Provider notice for a reference ClipCash did not issue.")
        return E.EXCEPTION, "unknown_reference", intent
    if status.status == "PENDING":
        return E.NO_CHANGE, "pending", intent
    if status.status == "FAILED":
        if intent.status in (S.CREATED, S.PENDING):
            _close(intent, S.FAILED, "provider_failed")
            record_business_event("payments.intent.failed", actor=None, target=intent,
                                  metadata={"reference": intent.reference, "reason": "provider_failed"})
            return E.ACCEPTED, "failed", intent
        return E.NO_CHANGE, "failed_ignored", intent
    if status.status == "CHARGEBACK":
        if intent.status == S.SETTLED:
            _open_dispute(intent, opened_by=None, note="Provider chargeback notice")
            return E.ACCEPTED, "chargeback", intent
        return E.NO_CHANGE, "chargeback_ignored", intent
    if status.status != "PAID":
        return E.NO_CHANGE, status.status.lower(), intent
    # PAID: provider confirmation, then reconciliation, then (if reported) settlement.
    if intent.status in (S.SETTLED, S.DISPUTED, S.REFUNDED):
        return E.NO_CHANGE, "already_settled", intent
    if intent.status in (S.EXPIRED, S.CANCELLED, S.FAILED):
        _exception(Exc.INTENT_CLOSED, intent=intent, provider_reference=provider_reference, amount=status.amount,
                   detail="Money reported for a closed payment; it must be returned to the payer.")
        return E.EXCEPTION, "intent_closed", intent
    if intent.status in (S.CREATED, S.PENDING):
        intent.status = S.PROVIDER_CONFIRMED
        intent.confirmed_at = timezone.now()
        intent.provider_amount = status.amount
        intent.provider_currency = status.currency
        intent.save()
    if intent.status == S.PROVIDER_CONFIRMED:
        problem = _reconcile(intent, status)
        if problem:
            return E.EXCEPTION, problem, intent
    if intent.status == S.RECONCILED and status.settled:
        _settle(intent)
    return E.ACCEPTED, "paid", intent


def _reconcile(intent, status):
    """Compare every field with the authoritative provider record. "" = reconciled."""
    checks = [
        (status.environment != intent.environment, Exc.ENVIRONMENT_MISMATCH),
        (status.intent_reference != intent.reference, Exc.REFERENCE_MISMATCH),
        (status.currency != intent.currency, Exc.CURRENCY_MISMATCH),
        (status.amount != intent.amount, Exc.AMOUNT_MISMATCH),
    ]
    for failed, reason in checks:
        if failed:
            _exception(reason, intent=intent, provider_reference=intent.provider_reference, amount=status.amount,
                       detail=f"Provider reported {status.currency} {status.amount}; expected "
                              f"{intent.currency} {intent.amount}.")
            record_business_event("payments.reconciliation.refused", actor=None, target=intent,
                                  metadata={"reference": intent.reference, "reason": reason})
            return reason
    if intent.purpose == Purpose.MEMBERSHIP:
        subscription = Subscription.objects.get(pk=intent.subscription_id)
        previewed = MembershipActivationPreview.objects.filter(
            subscription=subscription, decision__in=["WOULD_ACTIVATE", "WOULD_RENEW"]).exists()
        if previewed or subscription.status != Subscription.Status.PENDING:
            reason = Exc.DUPLICATE_PAYMENT if previewed else Exc.MEMBERSHIP_NOT_PENDING
            _exception(reason, intent=intent, provider_reference=intent.provider_reference, amount=status.amount,
                       detail="The membership was already paid or is no longer awaiting payment.")
            record_business_event("payments.reconciliation.refused", actor=None, target=intent,
                                  metadata={"reference": intent.reference, "reason": reason})
            return reason
    intent.status = S.RECONCILED
    intent.reconciled_at = timezone.now()
    intent.save()
    if intent.purpose == Purpose.MEMBERSHIP:
        _post("membership.payment_received", debit="PAYMENT_CLEARING", credit="MEMBERSHIP_DEFERRED_REVENUE",
              amount=intent.amount, intent=intent, key=f"{intent.pk}:received", domain=intent.funds_domain)
    else:
        _post("advertiser.funding_received", debit="PAYMENT_CLEARING", credit="ADVERTISER_FUNDING_PENDING",
              credit_scope=str(intent.organization_id), amount=intent.amount, intent=intent,
              key=f"{intent.pk}:received", domain=intent.funds_domain)
    record_business_event("payments.reconciliation.completed", actor=None, target=intent,
                          metadata={"reference": intent.reference, "amount": intent.amount})
    return ""


def _locked_funds(organization_id):
    SandboxAdvertiserFunds.objects.get_or_create(organization_id=organization_id)
    return SandboxAdvertiserFunds.objects.select_for_update().get(organization_id=organization_id)


def _settle(intent):
    intent.status = S.SETTLED
    intent.settled_at = timezone.now()
    intent.save()
    _post("payment.settled", debit="CASH", credit="PAYMENT_CLEARING", amount=intent.amount, intent=intent,
          key=f"{intent.pk}:settled", domain=intent.funds_domain)
    if intent.purpose == Purpose.ADVERTISER_DEPOSIT:
        # Provider settlement is the independent evidence that verifies the deposit.
        _post("advertiser.funding_verified", debit="ADVERTISER_FUNDING_PENDING", credit="ADVERTISER_FUNDING_VERIFIED",
              debit_scope=str(intent.organization_id), credit_scope=str(intent.organization_id),
              amount=intent.amount, intent=intent, key=f"{intent.pk}:verified", domain=intent.funds_domain)
        funds = _locked_funds(intent.organization_id)
        funds.received += intent.amount
        funds.save()
    else:
        _preview_membership(intent)
    record_business_event("payments.settlement.completed", actor=None, target=intent, metadata={
        "reference": intent.reference, "amount": intent.amount, "purpose": intent.purpose})


def _preview_membership(intent):
    """Record what activation WOULD do. Phase 03 subscriptions cannot mark sandbox
    entitlements, so the subscription itself is never changed."""
    if MembershipActivationPreview.objects.filter(intent=intent).exists():
        return
    subscription = Subscription.objects.select_related("renewal_of").get(pk=intent.subscription_id)
    D = MembershipActivationPreview.Decision
    now = timezone.now()
    if MembershipActivationPreview.objects.filter(subscription=subscription,
                                                  decision__in=[D.WOULD_ACTIVATE, D.WOULD_RENEW]).exists():
        decision, start = D.DUPLICATE_PAYMENT, None
    elif subscription.status != Subscription.Status.PENDING:
        decision, start = D.NOT_PENDING, None
    elif subscription.renewal_of and subscription.renewal_of.expires_at and subscription.renewal_of.expires_at > now:
        decision, start = D.WOULD_RENEW, subscription.renewal_of.expires_at  # Starts when the current one ends.
    else:
        decision, start = D.WOULD_ACTIVATE, now
    MembershipActivationPreview.objects.create(
        intent=intent, subscription=subscription, decision=decision, would_start=start,
        would_end=start + timedelta(days=subscription.duration_days) if start else None)
    record_business_event("payments.membership.preview", actor=None, target=intent,
                          metadata={"reference": intent.reference, "decision": decision})


# --- Allocations -----------------------------------------------------------------------------


def allocate(user, organization, campaign, *, amount):
    """Commit SANDBOX restricted funds to the advertiser's own campaign. Owners only.
    The campaign's real funding_status stays UNFUNDED."""
    require_sandbox()
    if not _account_ok(user) or user.is_staff or not _owner_of(user, organization):
        _deny("allocation.create", user, organization, "owner_required")
    try:
        amount = to_amount(amount)
    except (ValueError, TypeError):
        raise ValidationError({"amount": "Enter a whole number of shillings."}) from None
    if amount <= 0:
        raise ValidationError({"amount": "Enter an amount greater than zero."})
    refusal = None
    with transaction.atomic():
        funds = _locked_funds(organization.pk)  # Serializes allocations for this advertiser.
        campaign = Campaign.objects.select_for_update(of=("self",)).get(pk=campaign.pk)
        if campaign.organization_id != organization.pk:
            refusal = "other_advertiser"
        elif campaign.status not in ALLOCATABLE_CAMPAIGN_STATES:
            refusal = "campaign_not_approved"
        elif SandboxCampaignAllocation.objects.filter(campaign=campaign, status="ACTIVE").exists():
            refusal = "already_allocated"
        elif amount > funds.available:
            refusal = "insufficient_funds"
        else:
            funds.allocated += amount
            funds.save()
            allocation = SandboxCampaignAllocation.objects.create(organization=organization, campaign=campaign,
                                                                  amount=amount, created_by=user)
            _post("campaign.funding_committed", debit="ADVERTISER_FUNDING_VERIFIED",
                  credit="CAMPAIGN_RESTRICTED_FUNDING", debit_scope=str(organization.pk),
                  credit_scope=str(campaign.pk), amount=amount, intent=None, key=f"allocation:{allocation.pk}",
                  domain=FundsDomain.ADVERTISER_RESTRICTED_FUNDS)
            record_business_event("payments.allocation.created", actor=user, target=campaign,
                                  metadata={"campaign": str(campaign.pk), "amount": amount})
    if refusal:
        record_security_event("payments.allocation.refused", outcome=DENIED, actor=user, target=campaign,
                              metadata={"campaign": str(campaign.pk), "reason": refusal})
        messages = {"other_advertiser": "Funds can only be allocated to your own campaigns.",
                    "campaign_not_approved": "Only approved, running campaigns can receive funds.",
                    "already_allocated": "This campaign already has an allocation. Release it first.",
                    "insufficient_funds": "Not enough available sandbox funds."}
        raise InvalidState(messages[refusal], code=refusal)
    return allocation


def release_allocation(user, allocation):
    require_sandbox()
    if not (can_approve_refunds(user) or (_account_ok(user) and _owner_of(user, allocation.organization))):
        _deny("allocation.release", user, allocation.campaign)
    with transaction.atomic():
        funds = _locked_funds(allocation.organization_id)
        allocation = SandboxCampaignAllocation.objects.select_for_update().get(pk=allocation.pk)
        if allocation.status == SandboxCampaignAllocation.Status.RELEASED:
            return allocation
        funds.allocated -= allocation.amount
        funds.save()
        allocation.status = SandboxCampaignAllocation.Status.RELEASED
        allocation.released_at = timezone.now()
        allocation.released_by = user
        allocation.save()
        _post("campaign.funding_released", debit="CAMPAIGN_RESTRICTED_FUNDING", credit="ADVERTISER_FUNDING_VERIFIED",
              debit_scope=str(allocation.campaign_id), credit_scope=str(allocation.organization_id),
              amount=allocation.amount, intent=None, key=f"allocation:{allocation.pk}:released",
              domain=FundsDomain.ADVERTISER_RESTRICTED_FUNDS)
        record_business_event("payments.allocation.released", actor=user, target=allocation.campaign,
                              metadata={"campaign": str(allocation.campaign_id), "amount": allocation.amount})
    return allocation


# --- Refunds -----------------------------------------------------------------------------------


def request_refund(actor, intent, *, reason):
    """Staff request (first person). Deposit funds are frozen at once so they cannot be reused."""
    require_sandbox()
    _require(can_approve_refunds, actor, "refund.request", intent)
    reason = _reason(reason)
    with transaction.atomic():
        intent = PaymentIntent.objects.select_for_update().get(pk=intent.pk)
        if intent.status != S.SETTLED:
            raise InvalidState("Only settled payments can be refunded.")
        if intent.purpose == Purpose.ADVERTISER_DEPOSIT:
            funds = _locked_funds(intent.organization_id)
            if funds.available < intent.amount:
                raise InvalidState("These funds are already allocated to campaigns. Release allocations first.",
                                   code="funds_committed")
            funds.frozen += intent.amount
            funds.save()
        try:
            with transaction.atomic():
                refund = RefundRequest.objects.create(intent=intent, amount=intent.amount, reason=reason,
                                                      requested_by=actor)
        except IntegrityError:
            raise InvalidState("A refund for this payment already exists.", code="refund_exists") from None
        record_business_event("payments.refund.requested", actor=actor, target=intent,
                              metadata={"reference": intent.reference, "amount": intent.amount})
    return refund


def decide_refund(actor, refund, *, approve, note=""):
    """Second person approves (provider refund + reversing postings) or rejects."""
    require_sandbox()
    _require(can_approve_refunds, actor, "refund.decide", refund.intent)
    if refund.requested_by_id == actor.pk:
        _deny("refund.decide", actor, refund.intent, "four_eyes")
    provider = get_provider(refund.intent.provider)
    with transaction.atomic():
        refund = RefundRequest.objects.select_for_update().get(pk=refund.pk)
        intent = PaymentIntent.objects.select_for_update().get(pk=refund.intent_id)
        if refund.status != RefundRequest.Status.REQUESTED:
            return refund
        refund.decided_by = actor
        refund.decided_at = timezone.now()
        refund.decision_note = (note or "")[:300]
        if not approve:
            refund.status = RefundRequest.Status.REJECTED
            refund.save()
            if intent.purpose == Purpose.ADVERTISER_DEPOSIT:
                funds = _locked_funds(intent.organization_id)
                funds.frozen -= refund.amount
                funds.save()
            record_business_event("payments.refund.rejected", actor=actor, target=intent,
                                  metadata={"reference": intent.reference})
            return refund
        refund.provider_refund_reference = provider.refund(intent.provider_reference, refund.amount,
                                                           f"refund:{refund.pk}")  # Sandbox: local, no network.
        refund.status = RefundRequest.Status.COMPLETED
        refund.save()
        _reverse_funds(intent, refund.amount, key=f"refund:{refund.pk}")
        _close(intent, S.REFUNDED, "refunded")
        record_business_event("payments.refund.completed", actor=actor, target=intent,
                              metadata={"reference": intent.reference, "amount": refund.amount})
    return refund


def _reverse_funds(intent, amount, *, key):
    """Refund / chargeback postings and funds movement (frozen -> refunded). Never edits history."""
    if intent.purpose == Purpose.ADVERTISER_DEPOSIT:
        funds = _locked_funds(intent.organization_id)
        funds.frozen -= amount
        funds.refunded += amount
        funds.save()
        _post("advertiser.refund_approved", debit="ADVERTISER_FUNDING_VERIFIED", credit="REFUNDS_PAYABLE",
              debit_scope=str(intent.organization_id), amount=amount, intent=intent, key=f"{key}:approved",
              domain=intent.funds_domain)
    else:
        # Membership access is NOT clawed back automatically (no business policy yet).
        _post("membership.refund_approved", debit="MEMBERSHIP_DEFERRED_REVENUE", credit="REFUNDS_PAYABLE",
              amount=amount, intent=intent, key=f"{key}:approved", domain=intent.funds_domain)
    _post("refund.paid", debit="REFUNDS_PAYABLE", credit="PAYMENT_CLEARING", amount=amount, intent=intent,
          key=f"{key}:paid", domain=intent.funds_domain)


# --- Disputes ----------------------------------------------------------------------------------


def _open_dispute(intent, *, opened_by, note):
    """Inside a transaction holding the intent lock. Freezes what is still available;
    any committed remainder becomes a shortfall exception and flags allocations."""
    frozen = shortfall = 0
    if intent.purpose == Purpose.ADVERTISER_DEPOSIT:
        funds = _locked_funds(intent.organization_id)
        frozen = min(intent.amount, funds.available)
        shortfall = intent.amount - frozen
        funds.frozen += frozen
        funds.save()
        if shortfall:
            SandboxCampaignAllocation.objects.filter(organization_id=intent.organization_id, status="ACTIVE").update(
                flagged=True, flag_reason=f"Dispute on {intent.reference}")
            _exception(Exc.FUNDS_SHORTFALL, intent=intent, organization=intent.organization, amount=shortfall,
                       detail="Disputed funds were already allocated to campaigns. Manual intervention needed.")
    intent.status = S.DISPUTED
    intent.save()
    dispute = PaymentDispute.objects.create(intent=intent, opened_by=opened_by, frozen_amount=frozen,
                                            shortfall_amount=shortfall, note=note[:300])
    record_business_event("payments.dispute.opened", actor=opened_by, target=intent, metadata={
        "reference": intent.reference, "frozen": frozen, "shortfall": shortfall})
    return dispute


def resolve_dispute(actor, dispute, *, won, note):
    require_sandbox()
    _require(can_approve_refunds, actor, "dispute.resolve", dispute.intent)
    if dispute.opened_by_id == actor.pk:
        _deny("dispute.resolve", actor, dispute.intent, "four_eyes")
    note = _reason(note)
    with transaction.atomic():
        dispute = PaymentDispute.objects.select_for_update().get(pk=dispute.pk)
        intent = PaymentIntent.objects.select_for_update().get(pk=dispute.intent_id)
        if dispute.status != PaymentDispute.Status.OPEN:
            return dispute
        dispute.status = PaymentDispute.Status.WON if won else PaymentDispute.Status.LOST
        dispute.resolved_by = actor
        dispute.resolved_at = timezone.now()
        dispute.note = note
        dispute.save()
        if won:
            if intent.purpose == Purpose.ADVERTISER_DEPOSIT:
                funds = _locked_funds(intent.organization_id)
                funds.frozen -= dispute.frozen_amount
                funds.save()
            intent.status = S.SETTLED
            intent.save()
        else:
            amount = dispute.frozen_amount if intent.purpose == Purpose.ADVERTISER_DEPOSIT else intent.amount
            if amount:
                _reverse_funds(intent, amount, key=f"dispute:{dispute.pk}")
            _close(intent, S.REFUNDED, "charged_back")
        record_business_event("payments.dispute.resolved", actor=actor, target=intent, metadata={
            "reference": intent.reference, "outcome": dispute.status})
    return dispute


# --- Exceptions ----------------------------------------------------------------------------------


def resolve_exception(actor, exception, *, action, note):
    """Review actions never create funds: retry re-runs reconciliation against the
    provider's authoritative record; reject closes the payment as FAILED (any money
    must be returned outside ClipCash); dismiss records a note."""
    require_sandbox()
    _require(can_review_reconciliation, actor, "exception.resolve", exception.intent)
    note = _reason(note)
    if action not in ("retry", "reject_payment", "dismiss"):
        raise InvalidState("Unknown action.")
    if action == "retry" and exception.intent_id:
        intent = exception.intent
        process_provider_update(get_provider(intent.provider), f"review:{exception.pk}:{timezone.now().timestamp()}",
                                intent.provider_reference, kind=ProviderEvent.Kind.POLL)
    with transaction.atomic():
        exception = ReconciliationException.objects.select_for_update().get(pk=exception.pk)
        if exception.status != ReconciliationException.Status.OPEN:
            return exception
        if action == "retry":
            intent = PaymentIntent.objects.get(pk=exception.intent_id) if exception.intent_id else None
            if intent is None or intent.status == S.PROVIDER_CONFIRMED:
                raise InvalidState("Reconciliation still does not match. Reject or dismiss instead.",
                                   code="still_mismatched")
        if action == "reject_payment" and exception.intent_id:
            intent = PaymentIntent.objects.select_for_update().get(pk=exception.intent_id)
            if intent.status == S.PROVIDER_CONFIRMED:
                _close(intent, S.FAILED, "rejected_after_review")
                record_business_event("payments.intent.failed", actor=actor, target=intent,
                                      metadata={"reference": intent.reference, "reason": "rejected_after_review"})
        exception.status = (ReconciliationException.Status.DISMISSED if action == "dismiss"
                            else ReconciliationException.Status.RESOLVED)
        exception.resolved_by = actor
        exception.resolved_at = timezone.now()
        exception.resolution_note = note
        exception.save()
        record_business_event("payments.exception.resolved", actor=actor, target=exception.intent,
                              metadata={"reason": exception.reason, "action": action})
    return exception


# --- Jobs ----------------------------------------------------------------------------------------


def expire_intents(now=None, limit=500):
    now = now or timezone.now()
    count = 0
    with transaction.atomic():
        due = (PaymentIntent.objects.select_for_update(skip_locked=True)
               .filter(status__in=[S.CREATED, S.PENDING], expires_at__lte=now).order_by("expires_at")[:limit])
        for intent in due:
            _close(intent, S.EXPIRED, "expired")
            record_business_event("payments.intent.expired", actor=None, target=intent,
                                  metadata={"reference": intent.reference})
            count += 1
    return count


def poll_pending(limit=100):
    """Ask the provider about pending payments (bounded). Each status is applied once."""
    if not sandbox_active():
        return 0
    cutoff = timezone.now() - timedelta(seconds=settings.PAYMENTS_POLL_AFTER_SECONDS)
    recent = timezone.now() - timedelta(days=90)  # Chargeback window watched for settled payments.
    candidates = list(PaymentIntent.objects.filter(
        Q(status__in=[S.PENDING, S.RECONCILED]) | Q(status=S.SETTLED, settled_at__gte=recent), created_at__lte=cutoff)
        .exclude(provider_reference="").order_by("updated_at").values_list("provider", "provider_reference")[:limit])
    for provider_code, reference in candidates:
        provider = get_provider(provider_code)
        status = provider.fetch_status(reference)
        if status is None:
            continue
        process_provider_update(provider, f"poll:{reference}:{status.sequence}", reference,
                                kind=ProviderEvent.Kind.POLL)
    return len(candidates)


def check_funding():
    """Recompute every advertiser's sandbox funds from source records; mismatches go to review."""
    problems = 0
    for funds in SandboxAdvertiserFunds.objects.all():
        deposits = PaymentIntent.objects.filter(organization_id=funds.organization_id,
                                                purpose=Purpose.ADVERTISER_DEPOSIT)
        received = deposits.filter(settled_at__isnull=False).aggregate(t=Sum("amount", default=0))["t"]
        allocated = SandboxCampaignAllocation.objects.filter(organization_id=funds.organization_id,
                                                             status="ACTIVE").aggregate(t=Sum("amount", default=0))["t"]
        refund_pending = RefundRequest.objects.filter(intent__in=deposits, status="REQUESTED").aggregate(
            t=Sum("amount", default=0))["t"]
        dispute_frozen = PaymentDispute.objects.filter(intent__in=deposits, status="OPEN").aggregate(
            t=Sum("frozen_amount", default=0))["t"]
        refunded = (RefundRequest.objects.filter(intent__in=deposits, status="COMPLETED")
                    .aggregate(t=Sum("amount", default=0))["t"]
                    + PaymentDispute.objects.filter(intent__in=deposits, status="LOST")
                    .aggregate(t=Sum("frozen_amount", default=0))["t"])
        expected = (received, allocated, refund_pending + dispute_frozen, refunded)
        actual = (funds.received, funds.allocated, funds.frozen, funds.refunded)
        if expected != actual:
            problems += 1
            with transaction.atomic():
                _exception(Exc.FUNDING_INCONSISTENT, organization=funds.organization,
                           detail=f"Expected {expected}, recorded {actual}.")
    return problems


# --- Reporting -----------------------------------------------------------------------------------


def funds_for(organization):
    funds = SandboxAdvertiserFunds.objects.filter(organization=organization).first()
    if funds is None:
        return {"received": 0, "allocated": 0, "frozen": 0, "refunded": 0, "available": 0}
    return {"received": funds.received, "allocated": funds.allocated, "frozen": funds.frozen,
            "refunded": funds.refunded, "available": funds.available}


def member_intents(member):
    return (PaymentIntent.objects.select_related("subscription")
            .filter(payer=member, purpose=Purpose.MEMBERSHIP).order_by("-created_at"))


def payable_subscriptions(member):
    return (Subscription.objects.filter(member=member, status=Subscription.Status.PENDING,
                                        pending_until__gt=timezone.now()).order_by("-created_at"))

