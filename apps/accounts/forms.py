from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, PasswordResetForm
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.audit.services import DENIED, FAILURE, SUCCESS, hash_identifier, record_security_event

from . import services
from .throttling import (
    LOGIN_FAILURES_PER_ACCOUNT,
    LOGIN_FAILURES_PER_ACCOUNT_IP,
    LOGIN_FAILURES_PER_IP,
    client_ip_for_limits,
)

User = get_user_model()


def _email_field(**attrs):
    return forms.EmailField(
        label=_("Email address"),
        max_length=254,
        widget=forms.EmailInput(attrs={"autocomplete": "email", **attrs}),
    )


class RegistrationForm(forms.Form):
    """
    Public sign-up takes only an email address. The password is chosen after the
    owner follows the emailed link, so nobody can pre-set another person's
    password. There are no role, status or permission fields.
    """

    email = _email_field(autofocus=True)

    def clean_email(self):
        return User.objects.normalize_email(self.cleaned_data["email"])


class ThrottledLoginMixin:
    throttled_message = _("Too many failed sign-in attempts. Please wait a few minutes and try again.")
    interface = "web"

    def clean(self):
        account = User.objects.normalize_email(self.cleaned_data.get("username"))
        # None when the client cannot be told apart from other visitors (proxy
        # misconfiguration): per-IP limits are then skipped instead of becoming one
        # shared, site-wide limit; the per-account ceiling still applies.
        ip = client_ip_for_limits(self.request)
        # Never store what was typed: people sometimes type a password here.
        identifier = {"interface": self.interface, "identifier_hash": hash_identifier(account)}

        if (
            (ip and LOGIN_FAILURES_PER_ACCOUNT_IP.is_limited(account, ip))
            or LOGIN_FAILURES_PER_ACCOUNT.is_limited(account)
            or (ip and LOGIN_FAILURES_PER_IP.is_limited(ip))
        ):
            record_security_event("auth.login.throttled", outcome=DENIED, metadata=identifier)
            raise ValidationError(self.throttled_message, code="throttled")

        try:
            cleaned_data = super().clean()
        except ValidationError:
            if account:
                if ip:
                    LOGIN_FAILURES_PER_ACCOUNT_IP.hit(account, ip)
                LOGIN_FAILURES_PER_ACCOUNT.hit(account)
            if ip:
                LOGIN_FAILURES_PER_IP.hit(ip)
            target = User.objects.filter(email=account).first() if account else None
            record_security_event(
                "auth.login.failed",
                outcome=FAILURE,
                target=target,
                metadata={**identifier, "account_exists": target is not None},
            )
            raise

        user = self.get_user()
        if user is not None:
            if ip:
                LOGIN_FAILURES_PER_ACCOUNT_IP.reset(account, ip)
            record_security_event(
                "auth.login.succeeded",
                outcome=SUCCESS,
                actor=user,
                target=user,
                metadata={"interface": self.interface},
            )
        return cleaned_data


class LoginForm(ThrottledLoginMixin, AuthenticationForm):
    username = _email_field(autofocus=True)


class AdminLoginForm(ThrottledLoginMixin, AdminAuthenticationForm):
    interface = "admin"


class EmailRequestForm(forms.Form):
    email = _email_field()


class AccountPasswordResetForm(PasswordResetForm):
    def get_users(self, email):
        """Only ACTIVE accounts with a usable password can reset it.

        PENDING accounts recover by requesting a new activation link; SUSPENDED
        and DISABLED accounts cannot regain access through self-service.
        """
        users = User._default_manager.filter(
            email=User.objects.normalize_email(email), status=User.Status.ACTIVE
        )
        return (user for user in users if user.has_usable_password())

    def save(self, *, request=None, **kwargs):
        """Queue reset emails in the durable outbox instead of sending inline.
        The link token is generated at delivery, dated to this request."""
        from apps.notifications.services import base_url, enqueue_email

        from .emails import PASSWORD_RESET

        for user in self.get_users(self.cleaned_data["email"]):
            enqueue_email(PASSWORD_RESET, recipient=user.email, user=user,
                          context={"base_url": base_url(request)})


class AccessChangeFormMixin:
    """
    Server-side authorization for admin user forms. ``actor`` is set by the
    ModelAdmin. Runs before the instance is updated, so ``self.instance`` still
    holds the stored role and status.
    """

    actor = None

    def clean(self):
        cleaned_data = super().clean()
        services.validate_access_change(
            self.actor,
            self.instance,
            role=cleaned_data.get("role", self.instance.role),
            status=cleaned_data.get("status", self.instance.status),
        )
        return cleaned_data
