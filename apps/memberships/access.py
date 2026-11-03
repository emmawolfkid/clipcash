"""
Membership access decisions, for video playback (Phase 04) and viewing
verification (Phase 06).

Every check reads the database: it never trusts a cached status, and it does
not depend on the expiry job having run (a period that has ended denies access
immediately).
"""

from dataclasses import dataclass
from functools import wraps

from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.core import features

from .models import Subscription
from .services import member_eligibility


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str
    subscription: Subscription | None = None

    def __bool__(self):
        return self.allowed


def current_subscription(member, now=None):
    """The subscription whose period covers ``now`` (active or suspended), if any."""
    now = now or timezone.now()
    return (Subscription.objects.select_related("plan")
            .filter(member=member, status__in=[Subscription.Status.ACTIVE, Subscription.Status.SUSPENDED],
                    starts_at__lte=now, expires_at__gt=now)
            .order_by("-starts_at").first())


def check_access(member, entitlement=None, now=None):
    """
    Reasons: feature_disabled, not_authenticated, account_inactive,
    email_unverified, no_membership, membership_suspended, entitlement_missing, ok.
    """
    if not features.is_enabled("memberships"):
        return AccessDecision(False, "feature_disabled")
    # Read-only decision: an approved demo account still needs an active
    # membership, which it cannot obtain without a verified email.
    eligible, reason = member_eligibility(member, demo_waiver=True)
    if not eligible:
        return AccessDecision(False, reason)
    subscription = current_subscription(member, now)
    if subscription is None:
        return AccessDecision(False, "no_membership")
    if subscription.status == Subscription.Status.SUSPENDED:
        return AccessDecision(False, "membership_suspended", subscription)
    if entitlement is not None and entitlement not in subscription.entitlements:
        return AccessDecision(False, "entitlement_missing", subscription)
    return AccessDecision(True, "ok", subscription)


def membership_required(entitlement=None):
    """View decorator: 403 unless check_access() allows the request's user."""

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            decision = check_access(request.user, entitlement)
            if not decision:
                raise PermissionDenied(decision.reason)
            request.membership = decision.subscription
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator
