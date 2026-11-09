from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm, PasswordResetForm
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.audit.services import DENIED, FAILURE, SUCCESS, hash_identifier, record_security_event

from . import services
from .phone_numbers import normalize_phone_number
from .throttling import (
    CURRENT_PASSWORD_FAILURES,
    LOGIN_FAILURES_PER_ACCOUNT,
    LOGIN_FAILURES_PER_ACCOUNT_IP,
    LOGIN_FAILURES_PER_IP,
    PHONE_CHANGE_ATTEMPTS,
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


class CurrentPasswordMixin:
    """Check the signed-in owner's password before a sensitive change. Failures are
    limited per account and audited, so a stolen session cannot guess the password."""

    def verify_current_password(self, user, password, *, operation):
        if CURRENT_PASSWORD_FAILURES.is_limited(user.pk):
            record_security_event("auth.current_password.failed", outcome=DENIED, target=user,
                                  metadata={"operation": operation, "throttled": True})
            raise ValidationError(_("Too many incorrect passwords. Please wait a few minutes and try again."),
                                  code="throttled")
        if not user.check_password(password):
            CURRENT_PASSWORD_FAILURES.hit(user.pk)
            record_security_event("auth.current_password.failed", outcome=FAILURE, target=user,
                                  metadata={"operation": operation, "throttled": False})
            raise ValidationError(_("Your current password was entered incorrectly."), code="password_incorrect")
        CURRENT_PASSWORD_FAILURES.reset(user.pk)
        return password


class AccountPasswordChangeForm(CurrentPasswordMixin, PasswordChangeForm):
    def clean_old_password(self):
        return self.verify_current_password(self.user, self.cleaned_data["old_password"],
                                            operation="password_change")


class EmailChangeForm(CurrentPasswordMixin, forms.Form):
    new_email = forms.EmailField(
        label=_("New email address"), max_length=254,
        help_text=_("We will send a confirmation link there. Your sign-in address stays the same until you use it."),
        widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True}),
    )
    current_password = forms.CharField(
        label=_("Current password"), strip=False, widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_new_email(self):
        email = User.objects.normalize_email(self.cleaned_data["new_email"])
        if email == self.user.email:
            raise ValidationError(_("This is already your email address."), code="unchanged")
        return email

    def clean_current_password(self):
        return self.verify_current_password(self.user, self.cleaned_data["current_password"],
                                            operation="email_change")


class EmailChangeConfirmForm(CurrentPasswordMixin, forms.Form):
    """Completes an email-address change (RG-03). Asks for the current password unless this
    session confirmed it recently (apps.accounts.reauth)."""

    current_password = forms.CharField(
        label=_("Current password"), strip=False,
        help_text=_("For your security, confirm your password to finish the change."),
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password", "autofocus": True}),
    )

    def __init__(self, *args, user, require_password, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.require_password = require_password
        if not require_password:
            del self.fields["current_password"]

    def clean_current_password(self):
        return self.verify_current_password(self.user, self.cleaned_data["current_password"],
                                            operation="email_change_confirm")


class ProfileForm(forms.ModelForm):
    """Only these fields can be changed here: never role, status, email or verification."""

    class Meta:
        model = User
        fields = list(services.PROFILE_FIELDS)
        widgets = {
            "first_name": forms.TextInput(attrs={"autocomplete": "given-name"}),
            "last_name": forms.TextInput(attrs={"autocomplete": "family-name"}),
            "phone_number": forms.TextInput(attrs={"autocomplete": "tel", "inputmode": "tel"}),
        }
        error_messages = {"phone_number": {"unique": _("This phone number cannot be used. Please check it or contact support.")}}

    def clean_phone_number(self):
        phone = normalize_phone_number(self.cleaned_data.get("phone_number"))
        if phone is not None and phone != self.instance.phone_number:
            if PHONE_CHANGE_ATTEMPTS.is_limited(self.instance.pk):
                raise ValidationError(_("Too many phone number changes. Please try again later."), code="throttled")
            PHONE_CHANGE_ATTEMPTS.hit(self.instance.pk)
        return phone


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
