from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm, PasswordResetForm, SetPasswordForm
from django.core.exceptions import ValidationError
from django.utils.functional import lazy
from django.utils.translation import gettext_lazy as _

from apps.audit.services import DENIED, FAILURE, SUCCESS, hash_identifier, record_security_event

from . import services
from .password_validation import member_password_guidance_html, password_guidance_html
from .phone_numbers import identifier_kind, normalize_phone_number, normalize_tz_mobile
from .usernames import USERNAME_HELP_TEXT, normalize_username, validate_username
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


CONTACT_METHODS = (
    ("email", _("Email address")),
    ("phone", _("Mobile number")),
    ("username", _("Username")),
)
CONTACT_FIELDS = {"email": "email", "phone": "phone_number", "username": "username"}


class RegistrationForm(forms.Form):
    """
    PRD-03A: public member sign-up. First name, last name and password, and ONE sign-in
    identifier of the member's choice: an email address, a Tanzanian mobile number or a
    username. Only the chosen one is required and used; the other boxes are ignored. The
    member is signed in at once; nothing has to be confirmed first. There are no role,
    status or permission fields.
    """

    first_name = forms.CharField(
        label=_("First name"), max_length=150,
        widget=forms.TextInput(attrs={"autocomplete": "given-name", "autofocus": True}),
    )
    last_name = forms.CharField(
        label=_("Last name"), max_length=150,
        widget=forms.TextInput(attrs={"autocomplete": "family-name"}),
    )
    contact_method = forms.ChoiceField(
        label=_("Sign up with"), choices=CONTACT_METHODS, initial="email",
        help_text=_("Choose one, then fill in only that box. You will sign in with it."),
        widget=forms.RadioSelect,
    )
    email = forms.CharField(
        label=_("Email address"), max_length=254, required=False,
        help_text=_("You sign in with it, and a forgotten password can be reset by email to this address. Type it "
                    "carefully: whoever owns the address can reset the password."),
        widget=forms.EmailInput(attrs={"autocomplete": "email", "autocapitalize": "none", "spellcheck": "false"}),
    )
    phone_number = forms.CharField(
        label=_("Mobile number"), max_length=24, required=False,
        help_text=_("A Tanzanian mobile number, for example 0712 345 678. A mobile number cannot reset a forgotten "
                    "password: add and confirm an email address in My account for that."),
        widget=forms.TextInput(attrs={"type": "tel", "autocomplete": "tel", "inputmode": "tel"}),
    )
    username = forms.CharField(
        label=_("Username"), max_length=40, required=False, help_text=USERNAME_HELP_TEXT,
        widget=forms.TextInput(attrs={"autocomplete": "username", "autocapitalize": "none", "spellcheck": "false"}),
    )
    password1 = forms.CharField(
        label=_("Password"), strip=False, help_text=lazy(member_password_guidance_html, str)(),
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label=_("Confirm password"), strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def _clean_contact(self, method, raw):
        if method == "phone":
            return normalize_tz_mobile(raw)
        if method == "username":
            validate_username(raw)
            return normalize_username(raw)
        email = User.objects.normalize_email(raw)
        forms.EmailField().clean(email)  # Same format rule as every other email field.
        return email

    def clean(self):
        cleaned_data = super().clean()
        method = cleaned_data.get("contact_method")
        if method:
            field = CONTACT_FIELDS[method]
            raw = (self.data.get(self.add_prefix(field)) or "").strip()
            for other in CONTACT_FIELDS.values():  # Only the chosen contact is used.
                cleaned_data[other] = None
            if not raw:
                self.add_error(field, ValidationError(
                    _("Enter your %(contact)s, or choose another way to sign up.")
                    % {"contact": dict(CONTACT_METHODS)[method].lower()}, code="required"))
            else:
                try:
                    cleaned_data[field] = self._clean_contact(method, raw)
                except ValidationError as error:
                    self.add_error(field, error)
        password1, password2 = cleaned_data.get("password1"), cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            self.add_error("password2", ValidationError(_("The two passwords do not match."), code="password_mismatch"))
        elif password1:
            try:  # Similarity checks see the name and the identifier of the new member.
                password_validation.validate_password(password1, User(
                    first_name=cleaned_data.get("first_name") or "", last_name=cleaned_data.get("last_name") or "",
                    phone_number=cleaned_data.get("phone_number"), email=cleaned_data.get("email"),
                    username=cleaned_data.get("username"), role=User.Role.MEMBER))
            except ValidationError as error:
                self.add_error("password1", error)
        return cleaned_data

    @property
    def contact(self):
        """(contact method, normalized contact) after a successful is_valid()."""
        method = self.cleaned_data["contact_method"]
        return method, self.cleaned_data[CONTACT_FIELDS[method]]


def sign_in_key(value):
    """The normalized identifier a sign-in attempt is about (rate limits and audit): an E.164
    number for a phone-shaped value (when it parses), the lowercased username for a username,
    else the lowercased email. The three shapes never collide (apps.accounts.usernames)."""
    kind = identifier_kind(value)
    if value and kind == "phone":
        try:
            return normalize_phone_number(value) or ""
        except ValidationError:
            return " ".join(str(value).split()).lower()
    if kind == "username":
        return normalize_username(value) or ""
    return User.objects.normalize_email(value)


def _account_for(key):
    if not key:
        return None
    if key.startswith("+"):
        return User.objects.filter(phone_number=key).first()
    if identifier_kind(key) == "username":
        return User.objects.filter(username=key).first()
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
    """PRD-03A: members sign in with whatever they signed up with: their email address (any
    letter case), their mobile number (any accepted format) or their username (any letter
    case); staff keep signing in with their email address. Every failure gets the same
    message, so it does not say which identifiers exist."""

    username = forms.CharField(
        label=_("Mobile number, email or username"), max_length=254,
        widget=forms.TextInput(attrs={"autocomplete": "username", "autofocus": True, "autocapitalize": "none",
                                      "spellcheck": "false", "placeholder": _("0712 345 678, email or username")}),
    )
    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": _("We could not sign you in with those details. Check your email, mobile number or "
                           "username and your password (the password is case-sensitive)."),
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
        # PRD-03A: a confirmed address, or the address an email sign-up uses as its only identifier
        # (User.email_recovers_account). Never a mobile number or a username; never an unconfirmed
        # address on an account that signs in with something else.
        users = User._default_manager.filter(email=User.objects.normalize_email(email), status=User.Status.ACTIVE)
        return (user for user in users if user.email_recovers_account and user.has_usable_password())

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


class AccountPasswordGuidanceMixin:
    """REG-03: the new-password guidance describes the policy of THIS account (members: 8+
    characters with a letter and a number; staff: 10+). Validation itself is account-aware
    in apps.accounts.password_validation, whatever form sets the password."""

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["new_password1"].help_text = password_guidance_html(user)


class AccountSetPasswordForm(AccountPasswordGuidanceMixin, SetPasswordForm):
    """Password reset and account activation."""


class AccountPasswordChangeForm(AccountPasswordGuidanceMixin, CurrentPasswordMixin, PasswordChangeForm):
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
            # PRD-01: only an identifier that can actually sign in counts (an unconfirmed address of
            # an account created by public sign-up cannot), so removing the number never locks out.
            if not self._stored_account.email_signs_in and not self.instance.username:
                raise ValidationError(_("You sign in with this mobile number. Add and confirm an email address "
                                        "before removing it."), code="required_identifier")
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


class _NoEchoInput(forms.TextInput):
    """PAY-LIVE-01F-B: a one-time code is never written back into a page."""

    def format_value(self, value):
        return None


class PhoneCodeForm(forms.Form):
    """The 6-digit SMS code (apps.accounts.phone_verification checks it)."""

    code = forms.CharField(
        label=_("Verification code"), max_length=12, strip=True,
        widget=_NoEchoInput(attrs={"inputmode": "numeric", "autocomplete": "one-time-code", "pattern": "[0-9 ]*"}),
        help_text=_("The 6-digit code in the text message we sent you."),
    )
