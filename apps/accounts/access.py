"""Access-control helpers for function- and class-based views."""

from functools import wraps

from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.utils.translation import gettext_lazy as _


def identity_ready(user):
    """REG-02: may this ACTIVE account use member pages, choose a plan and pay?

    Members register with a mobile number and a password and use the account at once;
    the number is their sign-in identifier (unique, Tanzanian mobile format). Accounts
    that sign in with an email address only still need that address confirmed (the
    pre-REG-02 rule). Phone *ownership* (``phone_verified``) is tracked separately and is
    not required here; it is never accepted as proof for account recovery. Real-money
    steps stay protected by staff verification of every payment."""
    return bool(getattr(user, "email_verified", False) or getattr(user, "phone_number", None))


def demo_verification_optional():
    """True only when the restricted demo waives email verification: the flag is
    on, restricted demo mode is on, and no payment of any kind (live, manual or
    sandbox) is possible."""
    from apps.core import features
    from apps.payments.providers import sandbox_active

    return bool(
        settings.DEMO_EMAIL_VERIFICATION_OPTIONAL
        and settings.RESTRICTED_DEMO_MODE
        and not sandbox_active()
        and not settings.MANUAL_MEMBERSHIP_PAYMENTS_ENABLED
        and not any(features.is_enabled(key) for key in features.FINANCIAL_FEATURES)
    )


def email_verification_waived(user):
    """
    May this signed-in ACTIVE account use pages that require a verified email,
    without one? Only accounts listed in DEMO_ACCOUNTS, only while
    demo_verification_optional(). The email is never marked verified, and
    role checks and services that test email_verified themselves still apply.
    """
    if not (user.is_authenticated and user.status == user.Status.ACTIVE and demo_verification_optional()):
        return False
    approved = {email.strip().lower() for email in settings.DEMO_ACCOUNTS if email.strip()}
    return user.email.lower() in approved


# Shown where a workflow stays closed to waived accounts (see demo_unconfirmed).
DEMO_UNCONFIRMED_BLOCKED = _(
    "Not available to demo accounts without a confirmed email address: it involves payments, financial "
    "records or business verification, which stay restricted in this demonstration."
)


def demo_unconfirmed(user):
    """An approved demo account using the waiver: its email is still unconfirmed.
    Memberships, wallets, payout destinations, payments and advertiser actions keep
    requiring a confirmed email (apps.memberships.services.member_eligibility)."""
    return not identity_ready(user) and email_verification_waived(user)


def _deny_or_redirect(request, *, require_verified_email, roles=None):
    """Return a response that denies access, or None when access is allowed."""
    user = request.user
    if not user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    if user.status != user.Status.ACTIVE:
        # Defensive: the authentication backend already rejects these sessions.
        logout(request)
        return redirect_to_login(request.get_full_path())
    if require_verified_email and not identity_ready(user) and not email_verification_waived(user):
        return redirect("accounts:verification_required")
    if roles is not None and user.role not in roles:
        raise PermissionDenied
    return None


def verified_account_required(view_func):
    """Require an ACTIVE account with a verified email address."""

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        denied = _deny_or_redirect(request, require_verified_email=True)
        return denied or view_func(request, *args, **kwargs)

    return wrapper


def role_required(*roles, require_verified_email=True):
    """Require an ACTIVE account holding one of ``roles``; others get 403."""

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            denied = _deny_or_redirect(
                request, require_verified_email=require_verified_email, roles=roles
            )
            return denied or view_func(request, *args, **kwargs)

        return wrapper

    return decorator


class VerifiedAccountRequiredMixin:
    def dispatch(self, request, *args, **kwargs):
        denied = _deny_or_redirect(request, require_verified_email=True)
        return denied or super().dispatch(request, *args, **kwargs)


class RoleRequiredMixin:
    allowed_roles = ()
    require_verified_email = True

    def dispatch(self, request, *args, **kwargs):
        denied = _deny_or_redirect(
            request,
            require_verified_email=self.require_verified_email,
            roles=self.allowed_roles,
        )
        return denied or super().dispatch(request, *args, **kwargs)
