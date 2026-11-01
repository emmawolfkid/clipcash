import logging

from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, PasswordResetForm
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from . import services
from .throttling import (
    LOGIN_FAILURES_PER_ACCOUNT,
    LOGIN_FAILURES_PER_ACCOUNT_IP,
    LOGIN_FAILURES_PER_IP,
    client_ip,
)

security_log = logging.getLogger("clipcash.security")

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

    def clean(self):
        account = User.objects.normalize_email(self.cleaned_data.get("username"))
        ip = client_ip(self.request)

        if (
            LOGIN_FAILURES_PER_ACCOUNT_IP.is_limited(account, ip)
            or LOGIN_FAILURES_PER_ACCOUNT.is_limited(account)
            or LOGIN_FAILURES_PER_IP.is_limited(ip)
        ):
            security_log.warning("login_throttled ip=%s", ip)
            raise ValidationError(self.throttled_message, code="throttled")

        try:
            cleaned_data = super().clean()
        except ValidationError:
            if account:
                LOGIN_FAILURES_PER_ACCOUNT_IP.hit(account, ip)
                LOGIN_FAILURES_PER_ACCOUNT.hit(account)
            LOGIN_FAILURES_PER_IP.hit(ip)
            security_log.warning("login_failed ip=%s", ip)
            raise

        user = self.get_user()
        if user is not None:
            LOGIN_FAILURES_PER_ACCOUNT_IP.reset(account, ip)
            security_log.info("login_succeeded user=%s ip=%s", user.pk, ip)
        return cleaned_data


class LoginForm(ThrottledLoginMixin, AuthenticationForm):
    username = _email_field(autofocus=True)


class AdminLoginForm(ThrottledLoginMixin, AdminAuthenticationForm):
    pass


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
