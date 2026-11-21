"""
REG-03: password policy by account type (settings.AUTH_PASSWORD_VALIDATORS).

Members (public accounts):
    at least 8 characters, with at least one letter AND at least one number; spaces are allowed,
    so a short phrase with a number works.
Staff (Super Administrators, Content Managers) and accounts not yet known:
    the policy that applied before REG-03: at least 10 characters, no letter/number rule.
Everyone:
    Django's checks against personal information (name, username, email, mobile number),
    commonly used passwords and passwords made only of numbers.

The policy applies whenever a password is SET (registration, password change, reset, activation,
admin). Signing in never re-checks a stored password, so existing passwords keep working.
"""

from django.contrib.auth import password_validation
from django.contrib.auth.password_validation import MinimumLengthValidator
from django.core.exceptions import ValidationError
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

MEMBER_MIN_LENGTH = 8
STAFF_MIN_LENGTH = 10
LETTER_AND_NUMBER_MESSAGE = _("Your password must contain at least one letter and at least one number.")
_ANY = object()  # get_help_text() without an account (Django's own forms): describe both policies.


def is_member(user):
    """True for member accounts. Staff, and calls without an account, keep the staff policy."""
    return user is not None and getattr(user, "role", None) == "MEMBER"


class RoleMinimumLengthValidator(MinimumLengthValidator):
    """Django's minimum-length check, at 8 characters for members and 10 for everyone else."""

    def __init__(self, member_min_length=MEMBER_MIN_LENGTH, staff_min_length=STAFF_MIN_LENGTH):
        super().__init__(min_length=staff_min_length)
        self.member_min_length = member_min_length
        self.staff_min_length = staff_min_length

    def minimum_for(self, user):
        return self.member_min_length if is_member(user) else self.staff_min_length

    def validate(self, password, user=None):
        self.min_length = self.minimum_for(user)
        try:
            super().validate(password, user)
        finally:
            self.min_length = self.staff_min_length

    def get_help_text(self, user=_ANY):
        if user is _ANY:
            return _("Member passwords must contain at least %(member)d characters; staff passwords at least "
                     "%(staff)d.") % {"member": self.member_min_length, "staff": self.staff_min_length}
        minimum = self.minimum_for(user)
        return ngettext("Your password must contain at least %(min_length)d character.",
                        "Your password must contain at least %(min_length)d characters.",
                        minimum) % {"min_length": minimum}


class LetterAndNumberValidator:
    """Members only: at least one letter and at least one digit (any script; spaces allowed)."""

    def validate(self, password, user=None):
        if not is_member(user):
            return
        if not (any(char.isalpha() for char in password) and any(char.isdigit() for char in password)):
            raise ValidationError(LETTER_AND_NUMBER_MESSAGE, code="password_needs_letter_and_number")

    def get_help_text(self, user=_ANY):
        if user is _ANY:
            return _("Member passwords must contain at least one letter and at least one number.")
        return LETTER_AND_NUMBER_MESSAGE if is_member(user) else ""


def password_rules(user):
    """The rules the configured validators apply to ``user``'s account, in plain words."""
    rules = []
    for validator in password_validation.get_default_password_validators():
        try:
            text = validator.get_help_text(user=user)
        except TypeError:  # Django's own validators take no account.
            text = validator.get_help_text()
        if text:
            rules.append(text)
    return rules


def password_guidance_html(user=None):
    """Guidance shown under new-password fields for ``user``'s account (None: an account not yet
    known, which gets the staff policy), followed by every rule that applies to it."""
    if is_member(user):
        intro = _("Use at least %(min)d characters, with at least one letter and at least one number. Spaces are "
                  "allowed, so a short phrase of unrelated words with a number in it is easy to remember and hard "
                  "to guess. Avoid your name, username, phone number or email address.") % {"min": MEMBER_MIN_LENGTH}
    else:
        intro = _("Staff accounts need at least %(min)d characters. Avoid your name, email address or phone number, "
                  "and common passwords.") % {"min": STAFF_MIN_LENGTH}
    items = format_html_join("", "<li>{}</li>", ((rule,) for rule in password_rules(user)))
    return format_html("<p>{}</p><ul>{}</ul>", intro, items)


def member_password_guidance_html():
    """Guidance for public sign-up, where the new account is always a member."""
    from django.contrib.auth import get_user_model

    return password_guidance_html(get_user_model()(role="MEMBER"))
