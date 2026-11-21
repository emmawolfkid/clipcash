"""
REG-03: usernames, an optional sign-in identifier.

A username is stored lowercased, so it is unique ignoring case ("Amina.K" and "amina.k" are
one name). Its shape keeps the three sign-in identifiers apart, so the sign-in box can tell
which one was typed without asking:

* it starts with a letter, so it can never be read as a mobile number (digits, + and
  separators only);
* it never contains @, so it can never be read as an email address;
* 3 to 30 characters: ASCII letters, digits, dots and underscores; no two dots in a row and
  no dot at the end;
* a few names that could pass for ClipCash staff are reserved.
"""

import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 30
# Also enforced by a database check on the stored (lowercased) value.
USERNAME_PATTERN = r"^[a-z][a-z0-9._]{2,29}$"
USERNAME_RE = re.compile(USERNAME_PATTERN)
RESERVED_USERNAMES = frozenset({
    "admin", "administrator", "billing", "clipcash", "contact", "help", "info", "moderator", "none", "null",
    "payments", "root", "security", "staff", "superuser", "support", "system",
})

USERNAME_HELP_TEXT = _("3 to 30 characters: letters, numbers, dots (.) and underscores (_). It must start with a "
                       "letter. Capital letters do not matter: Amina.K and amina.k are the same username.")


def normalize_username(value):
    """The stored form of a username (trimmed, lowercased), or None when empty."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def validate_username(value):
    """Validate a username (in any letter case). Raises ValidationError with a specific message."""
    name = normalize_username(value) or ""
    if "@" in name:
        raise ValidationError(_("A username cannot contain @. To sign in with an email address, choose "
                                "“Email address” instead."), code="username_email")
    if name and not name[0].isalpha():
        raise ValidationError(_("A username must start with a letter, so it cannot be mistaken for a mobile "
                                "number. To sign in with your number, choose “Mobile number” instead."),
                              code="username_start")
    if not USERNAME_MIN_LENGTH <= len(name) <= USERNAME_MAX_LENGTH:
        raise ValidationError(_("A username must be between %(min)d and %(max)d characters long.")
                              % {"min": USERNAME_MIN_LENGTH, "max": USERNAME_MAX_LENGTH}, code="username_length")
    if not USERNAME_RE.match(name):
        raise ValidationError(_("Use only letters (a–z), numbers, dots (.) and underscores (_), with no spaces."),
                              code="username_characters")
    if ".." in name or name.endswith("."):
        raise ValidationError(_("A username cannot contain two dots in a row or end with a dot."),
                              code="username_dots")
    if name in RESERVED_USERNAMES:
        raise ValidationError(_("This username is reserved. Please choose another one."), code="username_reserved")
