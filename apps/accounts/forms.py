from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm, PasswordResetForm
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.audit.services import DENIED, FAILURE, SUCCESS, hash_identifier, record_security_event

from . import services
from .phone_numbers import looks_like_phone, normalize_phone_number, normalize_tz_mobile
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
    REG-02: public sign-up takes an active Tanzanian mobile number and a password, and the
    account is usable at once. Email is optional and is added (and confirmed) later from
    the account page. There are no role, status or permission fields.
    """

    phone_number = forms.CharField(
        label=_("Mobile number"), max_length=24,
        help_text=_("Your Tanzanian mobile number, for example 0712 345 678. You will sign in with it."),
        widget=forms.TextInput(attrs={"type": "tel", "autocomplete": "tel", "inputmode": "tel", "autofocus": True}),
    )
    password1 = forms.CharField(
        label=_("Password"), strip=False,
        help_text=password_validation.password_validators_help_text_html(),
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label=_("Confirm password"), strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def clean_phone_number(self):
        return normalize_tz_mobile(self.cleaned_data["phone_number"])

    def clean(self):
        cleaned_data = super().clean()
        password1, password2 = cleaned_data.get("password1"), cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            self.add_error("password2", ValidationError(_("The two passwords do not match."), code="password_mismatch"))
        elif password1:
            try:  # Similarity checks see the number the member will sign in with.
                password_validation.validate_password(
                    password1, User(phone_number=cleaned_data.get("phone_number"), role=User.Role.MEMBER))
            except ValidationError as error:
                self.add_error("password1", error)
        return cleaned_data


def sign_in_key(value):
    """The normalized identifier a sign-in attempt is about: an E.164 number for anything
    that is not an email address (when it parses), else the lowercased email."""
    if value and looks_like_phone(value):
        try:
            return normalize_phone_number(value) or ""
        except ValidationError:
            return " ".join(str(value).split()).lower()
    return User.objects.normalize_email(value)


def _account_for(key):
    if not key:
        return None
    if key.startswith("+"):
        return User.objects.filter(phone_number=key).first()
    return User.objects.filter(email=key).first()


class ThrottledLoginMixin:
    throttled_message = _("Too many failed sign-in attempts. Please wait a few minutes and try again.")
    interface = "web"

    def clean(self):
        account = sign_in_key(self.cleaned_data.get("username"))
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
            target = _account_for(account)
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
    """REG-02: members sign in with their mobile number (any accepted format) or, if they
    have one, their email address; staff keep signing in with their email address."""

    username = forms.CharField(
        label=_("Mobile number or email"), max_length=254,
        widget=forms.TextInput(attrs={"autocomplete": "username", "autofocus": True, "autocapitalize": "none",
                                      "spellcheck": "false"}),
    )
    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": _("Enter a correct mobile number or email and password. Both may be case-sensitive."),
    }


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
        # REG-02: only a VERIFIED address recovers an account. A phone number never does.
        users = User._default_manager.filter(
            email=User.objects.normalize_email(email), email_verified=True, status=User.Status.ACTIVE
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
        help_text=_("We will send a confirmation link there. Nothing changes until you use it."),
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


class ProfileForm(CurrentPasswordMixin, forms.ModelForm):
    """Only these fields can be changed here: never role, status, email or verification.

    REG-02: the mobile number is a sign-in identifier, so changing it needs the current
    password (like an email change), a new number must be a Tanzanian mobile number, and an
    account without an email address cannot remove it. The change resets phone_verified."""

    current_password = forms.CharField(
        label=_("Current password"), strip=False, required=False,
        help_text=_("Needed only when you change your mobile number."),
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )

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
        raw = self.cleaned_data.get("phone_number")
        phone = normalize_phone_number(raw)
        if phone == self.instance.phone_number:
            return phone
        if phone is None:
            if not self.instance.email:
                raise ValidationError(_("You sign in with this mobile number. Add an email address before removing "
                                        "it."), code="required_identifier")
            return None
        phone = normalize_tz_mobile(raw)
        if PHONE_CHANGE_ATTEMPTS.is_limited(self.instance.pk):
            raise ValidationError(_("Too many phone number changes. Please try again later."), code="throttled")
        PHONE_CHANGE_ATTEMPTS.hit(self.instance.pk)
        return phone

    def clean(self):
        cleaned_data = super().clean()
        if "phone_number" in cleaned_data and cleaned_data["phone_number"] != self._stored_phone:
            try:
                self.verify_current_password(self._stored_account, cleaned_data.get("current_password") or "",
                                             operation="phone_change")
            except ValidationError as error:
                self.add_error("current_password", error)
        return cleaned_data

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The instance is updated during validation; keep what is stored for the checks.
        self._stored_phone = self.instance.phone_number
        self._stored_account = self.instance


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
