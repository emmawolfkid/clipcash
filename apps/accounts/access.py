"""Access-control helpers for function- and class-based views."""

from functools import wraps

from django.contrib.auth import logout
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect


def _deny_or_redirect(request, *, require_verified_email, roles=None):
    """Return a response that denies access, or None when access is allowed."""
    user = request.user
    if not user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    if user.status != user.Status.ACTIVE:
        # Defensive: the authentication backend already rejects these sessions.
        logout(request)
        return redirect_to_login(request.get_full_path())
    if require_verified_email and not user.email_verified:
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
