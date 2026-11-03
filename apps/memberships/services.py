"""
Membership services: the only supported way to change plans and subscriptions.

Policies (see docs/architecture/memberships.md):

* One membership period at a time; renewals and plan changes start when the
  current period ends (no proration). At most one queued period, and only
  within MEMBERSHIPS_RENEWAL_WINDOW_DAYS of the current expiry.
* Members choose a plan, creating a PENDING subscription that snapshots the
  plan's current terms. It stays pending (honouring the quoted price) until a
  verified payment activates it or MEMBERSHIPS_PENDING_DAYS pass.
* Paid activation happens ONLY in activate_with_verified_payment(), which
  requires the live_payments feature (locked off), a registered payment
  verifier that independently confirms the payment, an exact amount match and
  a unique reference. Nothing else can mark a subscription paid.
* Complimentary grants are a separate, explicitly labelled policy: Super
  Administrator only, a reason, amount paid 0, and disabled unless
  MEMBERSHIPS_COMPLIMENTARY_GRANTS_ENABLED.
* Cancellation is immediate and ends access. Members may cancel only their own
  pending choices; administrators cancel active memberships. No automatic
  renewal and no automatic refunds.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from apps.audit.services import DENIED, FAILURE, record_business_event, record_security_event
from apps.core import features

from .models import MembershipPlan, MembershipPlanVersion, Subscription

S = Subscription.Status
R = Subscription.Reason
PLAN_TERMS = ("name", "price_amount", "duration_days", "benefits", "entitlements")
PLAN_EDITABLE = PLAN_TERMS + ("description", "display_order", "is_public")


class MembershipError(Exception):
    code = "membership_error"

    def __init__(self, message, *, code=None):
        super().__init__(message)
        if code:
            self.code = code


class NotAuthorized(MembershipError):
    code = "not_authorized"


class NotEligible(MembershipError):
    code = "not_eligible"


class PlanUnavailable(MembershipError):
    code = "plan_unavailable"


class InvalidTransition(MembershipError):
    code = "invalid_transition"


class RenewalNotAllowed(MembershipError):
    code = "renewal_not_allowed"


class PaymentVerificationError(MembershipError):
    code = "payment_not_verified"


def _feature_enabled(key):
    """Single gate the tests replace; live_payments is locked off in code."""
    return features.is_enabled(key)


# --- Authorization ---------------------------------------------------------------


def is_super_admin(user):
    User = get_user_model()
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.is_active
                and user.is_superuser and user.role == User.Role.SUPER_ADMIN)


def can_manage_subscriptions(user):
    return is_super_admin(user) or bool(
        user is not None and getattr(user, "is_authenticated", False) and user.is_active and user.is_staff
        and user.has_perm("memberships.manage_memberships")
    )


def _deny(action, error, actor, target=None):
    record_security_event(
        "membership.action.denied", outcome=DENIED,
        actor=actor if getattr(actor, "is_authenticated", False) else None, target=target,
        metadata={"action": action, "reason": error.code},
    )


def member_eligibility(member, *, demo_waiver=False):
    """(eligible, reason). Only ACTIVE accounts with a verified email qualify.

    ``demo_waiver=True`` also accepts an approved restricted-demo account
    (apps.accounts.access.email_verification_waived). Only read-only access
    decisions pass it; choosing or activating a membership, wallets, payments,
    payouts and advertiser actions never do, so they keep requiring a verified email.
    """
    from apps.accounts.access import email_verification_waived

    User = get_user_model()
    if member is None or not getattr(member, "is_authenticated", False):
        return False, "not_authenticated"
    if member.status != User.Status.ACTIVE or not member.is_active:
        return False, "account_inactive"
    if not member.email_verified and not (demo_waiver and email_verification_waived(member)):
        return False, "email_unverified"
    return True, "ok"


# --- Plans -----------------------------------------------------------------------


def _clean_plan_values(values):
    values = dict(values)
    for key in ("name", "description"):
        if key in values:
            values[key] = (values[key] or "").strip()
    if "benefits" in values:
        values["benefits"] = [str(b).strip() for b in (values["benefits"] or []) if str(b).strip()]
    if "entitlements" in values:
        values["entitlements"] = list(dict.fromkeys(values["entitlements"] or []))
    return values


def _snapshot_version(plan, actor):
    return MembershipPlanVersion.objects.create(
        plan=plan, version=plan.version, name=plan.name, price_amount=plan.price_amount,
        currency=plan.currency, duration_days=plan.duration_days, benefits=list(plan.benefits),
        entitlements=list(plan.entitlements), created_by_id=getattr(actor, "pk", None),
    )


def create_plan(actor, *, code, **values):
    if not is_super_admin(actor):
        error = NotAuthorized("Only Super Administrators manage membership plans.")
        _deny("plan.create", error, actor)
        raise error
    unknown = set(values) - set(PLAN_EDITABLE)
    if unknown:
        raise ValidationError(f"Unknown plan fields: {sorted(unknown)}")
    with transaction.atomic():
        plan = MembershipPlan(code=code, **_clean_plan_values(values))
        plan.full_clean()
        plan.save()
        _snapshot_version(plan, actor)
        record_business_event(
            "membership.plan.created", actor=actor, target=plan,
            metadata={"code": plan.code, "price_amount": plan.price_amount, "duration_days": plan.duration_days},
        )
    return plan


def update_plan(actor, plan, changes):
    """Edit a plan. Commercial terms create a new immutable version; existing
    subscriptions keep their own snapshots."""
    if not is_super_admin(actor):
        error = NotAuthorized("Only Super Administrators manage membership plans.")
        _deny("plan.update", error, actor, plan)
        raise error
    unknown = set(changes) - set(PLAN_EDITABLE)
    if unknown:
        raise ValidationError(f"These plan fields cannot be changed: {sorted(unknown)}")
    with transaction.atomic():
        plan = MembershipPlan.objects.select_for_update().get(pk=plan.pk)
        if plan.status == MembershipPlan.Status.ARCHIVED:
            raise PlanUnavailable("Archived plans cannot be edited.")
        changes = _clean_plan_values(changes)
        before = {name: getattr(plan, name) for name in changes}
        for name, value in changes.items():
            setattr(plan, name, value)
        plan.full_clean()
        changed = sorted(name for name in changes if getattr(plan, name) != before[name])
        if not changed:
            return plan
        terms_changed = any(name in PLAN_TERMS for name in changed)
        if terms_changed:
            plan.version += 1
        plan.save()
        if terms_changed:
            _snapshot_version(plan, actor)
        record_business_event(
            "membership.plan.updated", actor=actor, target=plan,
            metadata={"fields": changed, "version": plan.version,
                      "price_amount": plan.price_amount, "new_version": terms_changed},
        )
    return plan


def set_plan_status(actor, plan, status):
    if not is_super_admin(actor):
        error = NotAuthorized("Only Super Administrators manage membership plans.")
        _deny("plan.status", error, actor, plan)
        raise error
    if status not in MembershipPlan.Status.values:
        raise ValidationError("Unknown plan status.")
    with transaction.atomic():
        plan = MembershipPlan.objects.select_for_update().get(pk=plan.pk)
        if plan.status == status:
            return plan
        if plan.status == MembershipPlan.Status.ARCHIVED:
            raise PlanUnavailable("Archived plans cannot be reactivated.")
        old = plan.status
        plan.status = status
        plan.save(update_fields=["status", "updated_at"])
        record_business_event("membership.plan.status_changed", actor=actor, target=plan,
                              metadata={"from_status": old, "to_status": status})
    return plan


# --- Choosing a plan (pending subscription) ---------------------------------------


def _lock_member(member):
    """Serialize all membership changes for one member."""
    return get_user_model().objects.select_for_update().get(pk=member.pk)


def _current_and_queued(member, now):
    live = list(Subscription.objects.filter(member=member, status__in=[S.ACTIVE, S.SUSPENDED],
                                            expires_at__gt=now).order_by("starts_at"))
    current = next((s for s in live if s.starts_at <= now), None)
    queued = next((s for s in live if s.starts_at > now), None)
    return current, queued


def _new_pending(member, plan, now, renewal_of):
    version = plan.versions.get(version=plan.version)
    return Subscription.objects.create(
        member=member, plan=plan, plan_version=version, plan_name=version.name,
        price_amount=version.price_amount, currency=version.currency, duration_days=version.duration_days,
        benefits=list(version.benefits), entitlements=list(version.entitlements),
        pending_until=now + timedelta(days=settings.MEMBERSHIPS_PENDING_DAYS), renewal_of=renewal_of,
    )


def _prepare_pending(member, plan, now, *, enforce_window):
    """Inside a transaction with the member locked."""
    current, queued = _current_and_queued(member, now)
    if current is not None and current.status == S.SUSPENDED:
        raise NotEligible("Your membership is suspended.", code="membership_suspended")
    if queued is not None:
        raise RenewalNotAllowed("A renewal is already scheduled.", code="already_renewed")
    if current is not None and enforce_window:
        window = timedelta(days=settings.MEMBERSHIPS_RENEWAL_WINDOW_DAYS)
        if current.expires_at - now > window:
            raise RenewalNotAllowed(
                f"Renewal opens {settings.MEMBERSHIPS_RENEWAL_WINDOW_DAYS} days before your membership ends.",
                code="too_early")
    existing = Subscription.objects.select_for_update().filter(member=member, status=S.PENDING).first()
    if existing is not None:
        if existing.plan_version_id == plan.versions.get(version=plan.version).pk and existing.pending_until > now:
            return existing, False
        _transition(existing, S.CANCELLED, actor=None, now=now, reason=R.REPLACED, audit_actor=member)
    return _new_pending(member, plan, now, renewal_of=current), True


def choose_plan(member, plan):
    """A member selects a plan: creates (or returns) their PENDING subscription."""
    try:
        if not _feature_enabled("memberships"):
            raise PlanUnavailable("Memberships are not available.", code="feature_disabled")
        eligible, reason = member_eligibility(member)
        if not eligible:
            raise NotEligible("Your account cannot take a membership yet.", code=reason)
        now = timezone.now()
        with transaction.atomic():
            member = _lock_member(member)
            plan = MembershipPlan.objects.get(pk=plan.pk)
            if not plan.purchasable:
                raise PlanUnavailable("This plan is not available.")
            subscription, created = _prepare_pending(member, plan, now, enforce_window=True)
            if created:
                record_business_event(
                    "membership.subscription.created", actor=member, target=subscription,
                    metadata={"plan_code": plan.code, "price_amount": subscription.price_amount,
                              "renewal": subscription.renewal_of_id is not None},
                )
        return subscription
    except MembershipError as error:
        _deny("subscription.choose", error, member)
        raise


# --- Transitions -----------------------------------------------------------------

AUDIT_ACTIONS = {
    S.ACTIVE: "membership.subscription.activated",
    S.CANCELLED: "membership.subscription.cancelled",
    S.SUSPENDED: "membership.subscription.suspended",
    S.EXPIRED: "membership.subscription.expired",
}
ALLOWED = {
    S.PENDING: {S.ACTIVE, S.CANCELLED},
    S.ACTIVE: {S.SUSPENDED, S.EXPIRED, S.CANCELLED},
    S.SUSPENDED: {S.ACTIVE, S.EXPIRED, S.CANCELLED},
}


def _transition(subscription, new_status, *, actor, now, reason="", audit_actor=None, metadata=None,
                extra_fields=()):
    """Apply one state change (one UPDATE, with any ``extra_fields`` already set on
    the instance) and its audit event. Caller holds the row lock."""
    old = subscription.status
    if new_status not in ALLOWED.get(old, set()):
        raise InvalidTransition(f"A {old.lower()} subscription cannot become {new_status.lower()}.")
    subscription.status = new_status
    fields = ["status", "updated_at", *extra_fields]
    if new_status == S.CANCELLED:
        subscription.cancelled_at, subscription.cancellation_reason = now, reason
        fields += ["cancelled_at", "cancellation_reason"]
    elif new_status == S.SUSPENDED:
        subscription.suspended_at, subscription.suspension_reason = now, reason
        fields += ["suspended_at", "suspension_reason"]
    elif new_status == S.EXPIRED:
        subscription.expired_at = now
        fields += ["expired_at"]
    subscription.save(update_fields=fields)
    action = AUDIT_ACTIONS[new_status]
    if old == S.SUSPENDED and new_status == S.ACTIVE:
        action = "membership.subscription.reinstated"
    record_business_event(
        action, actor=audit_actor if audit_actor is not None else actor, target=subscription,
        metadata={"from_status": old, "to_status": new_status, "reason": reason or "",
                  "plan_code": subscription.plan.code, **(metadata or {})},
    )
    return subscription


def _activate(subscription, *, source, payment_reference, amount_paid, actor, now, period=None):
    """PENDING -> ACTIVE. Caller holds the member and subscription locks."""
    if subscription.status != S.PENDING:
        raise InvalidTransition("Only pending subscriptions can be activated.", code="not_pending")
    if subscription.pending_until <= now:
        raise InvalidTransition("This membership choice has expired.", code="choice_expired")
    if subscription.plan.status != MembershipPlan.Status.ACTIVE:
        raise PlanUnavailable("The plan is no longer available.")
    eligible, reason = member_eligibility(subscription.member)
    if not eligible:
        raise NotEligible("The member's account is not eligible.", code=reason)
    latest_end = (Subscription.objects.filter(member=subscription.member, status__in=[S.ACTIVE, S.SUSPENDED],
                                              expires_at__gt=now)
                  .aggregate(end=Max("expires_at"))["end"])
    start = max(now, latest_end) if latest_end else now
    subscription.starts_at = start
    subscription.expires_at = start + (period or timedelta(days=subscription.duration_days))
    subscription.activation_source = source
    subscription.payment_reference = payment_reference
    subscription.amount_paid = amount_paid
    subscription.activated_at = now
    subscription.activated_by_id = getattr(actor, "pk", None)
    # Activation data and the status change must be one UPDATE (database rule).
    return _transition(subscription, S.ACTIVE, actor=actor, now=now,
                       metadata={"source": source, "queued": start > now},
                       extra_fields=["starts_at", "expires_at", "activation_source", "payment_reference",
                                     "amount_paid", "activated_at", "activated_by_id"])


# --- Payment integration boundary ----------------------------------------------------


@dataclass(frozen=True)
class VerifiedPayment:
    """
    A payment confirmed by a trusted source. Created ONLY by the future payments
    module (Phase 08) after verifying a provider webhook signature and/or
    querying the provider. ``verifier`` names a function registered with
    register_payment_verifier(), which re-confirms the payment before use.
    """

    verifier: str
    reference: str
    amount: int
    currency: str
    confirmed_at: datetime


PAYMENT_VERIFIERS = {}


def register_payment_verifier(name, verify):
    """Register ``verify(payment) -> bool``. No verifier exists in this phase."""
    PAYMENT_VERIFIERS[name] = verify


def activate_with_verified_payment(subscription_id, payment):
    try:
        if not _feature_enabled("live_payments"):
            raise PaymentVerificationError("Live payments are not enabled.", code="payments_disabled")
        if not isinstance(payment, VerifiedPayment):
            raise PaymentVerificationError("A VerifiedPayment is required.")
        verify = PAYMENT_VERIFIERS.get(payment.verifier)
        if verify is None:
            raise PaymentVerificationError("Unknown payment verifier.", code="unknown_verifier")
        if not payment.reference or len(payment.reference) > 100:
            raise PaymentVerificationError("A payment reference is required.")
        if verify(payment) is not True:
            raise PaymentVerificationError("The payment could not be verified.")
        now = timezone.now()
        with transaction.atomic():
            subscription = Subscription.objects.select_related("plan").get(pk=subscription_id)
            _lock_member(subscription.member)
            subscription = Subscription.objects.select_for_update().select_related("plan", "member").get(
                pk=subscription_id)
            if payment.currency != subscription.currency or payment.amount != subscription.price_amount:
                raise PaymentVerificationError("The payment amount does not match the quoted price.",
                                               code="amount_mismatch")
            if Subscription.objects.filter(payment_reference=payment.reference).exists():
                raise PaymentVerificationError("This payment reference was already used.",
                                               code="duplicate_reference")
            return _activate(subscription, source=Subscription.Source.PAYMENT,
                             payment_reference=payment.reference, amount_paid=payment.amount,
                             actor=None, now=now)
    except IntegrityError as error:
        rejection = PaymentVerificationError("The activation conflicted with an existing record.",
                                             code="conflict")
        _record_activation_rejection(subscription_id, rejection)
        raise rejection from error
    except MembershipError as error:
        _record_activation_rejection(subscription_id, error)
        raise


def _record_activation_rejection(subscription_id, error):
    record_security_event(
        "membership.activation.rejected", outcome=DENIED if isinstance(error, PaymentVerificationError) else FAILURE,
        actor=None, metadata={"reason": error.code, "subscription": str(subscription_id)},
    )


# --- Complimentary grants (explicit, separately authorized policy) ---------------------


def grant_complimentary(actor, member, plan, *, period=None):
    """Super Administrator grants a free membership (amount paid 0, audited).

    Disabled unless MEMBERSHIPS_COMPLIMENTARY_GRANTS_ENABLED. ``period`` (a
    shorter test period) is accepted only when DEBUG is on.
    """
    try:
        if not settings.MEMBERSHIPS_COMPLIMENTARY_GRANTS_ENABLED:
            raise NotAuthorized("Complimentary grants are disabled.", code="grants_disabled")
        if not is_super_admin(actor):
            raise NotAuthorized("Only Super Administrators can grant complimentary memberships.")
        if period is not None and not settings.DEBUG:
            raise NotAuthorized("Custom periods are a development-only option.", code="debug_only")
        now = timezone.now()
        with transaction.atomic():
            member = _lock_member(member)
            plan = MembershipPlan.objects.get(pk=plan.pk)
            if plan.status != MembershipPlan.Status.ACTIVE:
                raise PlanUnavailable("The plan is not active.")
            subscription, created = _prepare_pending(member, plan, now, enforce_window=False)
            if created:
                record_business_event(
                    "membership.subscription.created", actor=actor, target=subscription,
                    metadata={"plan_code": plan.code, "price_amount": subscription.price_amount,
                              "renewal": subscription.renewal_of_id is not None},
                )
            return _activate(subscription, source=Subscription.Source.COMPLIMENTARY, payment_reference="",
                             amount_paid=0, actor=actor, now=now, period=period)
    except MembershipError as error:
        _deny("subscription.grant", error, actor)
        raise


# --- Cancellation, suspension, reinstatement -------------------------------------------


def _locked(subscription):
    return Subscription.objects.select_for_update().select_related("plan").get(pk=subscription.pk)


def cancel_pending(member, subscription):
    """A member withdraws their own unpaid choice."""
    try:
        if subscription.member_id != getattr(member, "pk", None):
            raise NotAuthorized("You can only cancel your own membership choice.")
        with transaction.atomic():
            subscription = _locked(subscription)
            if subscription.status != S.PENDING:
                raise InvalidTransition("Only pending choices can be cancelled here.")
            return _transition(subscription, S.CANCELLED, actor=member, now=timezone.now(),
                               reason=R.MEMBER_REQUEST)
    except MembershipError as error:
        _deny("subscription.cancel_pending", error, member, subscription)
        raise


ADMIN_REASONS = {R.ADMIN_REVIEW, R.POLICY_VIOLATION, R.FRAUD_SUSPECTED, R.REFUND, R.ACCOUNT_CLOSED,
                 R.MEMBER_REQUEST}


def _admin_change(actor, subscription, new_status, reason, action):
    try:
        if not can_manage_subscriptions(actor):
            raise NotAuthorized("You are not allowed to manage subscriptions.")
        if new_status != S.ACTIVE and reason not in ADMIN_REASONS:
            raise ValidationError("A valid reason is required.")
        now = timezone.now()
        with transaction.atomic():
            subscription = _locked(subscription)  # Decide on the locked row, never a stale copy.
            if new_status == S.ACTIVE:
                if subscription.status != S.SUSPENDED:
                    raise InvalidTransition("Only suspended subscriptions can be reinstated.")
                if subscription.expires_at <= now:
                    return _transition(subscription, S.EXPIRED, actor=actor, now=now)
            return _transition(subscription, new_status, actor=actor, now=now, reason=reason)
    except MembershipError as error:
        _deny(action, error, actor, subscription)
        raise


def suspend(actor, subscription, reason):
    return _admin_change(actor, subscription, S.SUSPENDED, reason, "subscription.suspend")


def reinstate(actor, subscription):
    """SUSPENDED -> ACTIVE; the period is not extended (time kept running)."""
    return _admin_change(actor, subscription, S.ACTIVE, "", "subscription.reinstate")


def cancel(actor, subscription, reason):
    """Administrator cancellation: immediate; no automatic refund."""
    return _admin_change(actor, subscription, S.CANCELLED, reason, "subscription.cancel")


# --- Expiry (background job) ---------------------------------------------------------


def process_expiry(now=None, limit=500):
    """Expire ended periods and cancel unpaid choices. Idempotent and safe to run
    concurrently (rows are claimed with SKIP LOCKED)."""
    now = now or timezone.now()
    counts = {"expired": 0, "cancelled": 0}
    with transaction.atomic():
        due = (Subscription.objects.select_for_update(skip_locked=True).select_related("plan")
               .filter(status__in=[S.ACTIVE, S.SUSPENDED], expires_at__lte=now).order_by("expires_at")[:limit])
        for subscription in due:
            _transition(subscription, S.EXPIRED, actor=None, now=now)
            counts["expired"] += 1
        stale = (Subscription.objects.select_for_update(skip_locked=True).select_related("plan")
                 .filter(status=S.PENDING, pending_until__lte=now).order_by("pending_until")[:limit])
        for subscription in stale:
            _transition(subscription, S.CANCELLED, actor=None, now=now, reason=R.UNPAID_TIMEOUT)
            counts["cancelled"] += 1
    return counts
