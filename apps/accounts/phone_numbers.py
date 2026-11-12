"""Phone number normalization to E.164 (``+<country code><national number>``)."""

import re
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

E164_PATTERN = r"^\+[1-9][0-9]{7,14}$"
E164_RE = re.compile(E164_PATTERN)

# Separators people commonly type between digit groups.
_SEPARATORS_RE = re.compile(r"[\s\-().]")


@dataclass(frozen=True)
class CountryRule:
    calling_code: str
    national_number_length: int


# Countries with an explicit national-number rule. Numbers for other calling codes
# are accepted when they are valid E.164.
COUNTRY_RULES = {
    "255": CountryRule(calling_code="255", national_number_length=9),  # Tanzania
}

DEFAULT_CALLING_CODE = "255"


def _invalid():
    return ValidationError(
        _("Enter a valid phone number, for example +255712345678 or 0712345678."),
        code="invalid_phone_number",
    )


def normalize_phone_number(value, default_calling_code=DEFAULT_CALLING_CODE):
    """
    Return ``value`` in E.164 format, or ``None`` when it is empty.

    Accepted input forms (for the default calling code 255):
        +255712345678, 00255712345678, 255712345678, 0712345678,
        optionally with spaces, dashes, dots or parentheses.
    """
    if value is None:
        return None

    compact = _SEPARATORS_RE.sub("", str(value))
    if not compact:
        return None

    default_rule = COUNTRY_RULES.get(default_calling_code)

    if compact.startswith("+"):
        digits = compact[1:]
    elif compact.startswith("00"):
        digits = compact[2:]
    elif compact.startswith("0") and default_rule:
        # National format with trunk prefix 0.
        digits = default_calling_code + compact[1:]
    elif (
        default_rule
        and compact.startswith(default_calling_code)
        and len(compact) == len(default_calling_code) + default_rule.national_number_length
    ):
        # International format without the leading "+".
        digits = compact
    else:
        raise _invalid()

    if not digits.isdigit() or not E164_RE.match("+" + digits):
        raise _invalid()

    for calling_code, rule in COUNTRY_RULES.items():
        if digits.startswith(calling_code):
            national = digits[len(calling_code):]
            if len(national) != rule.national_number_length or national.startswith("0"):
                raise _invalid()
            break

    return "+" + digits


def validate_e164(value):
    """Validate a stored value that must already be normalized."""
    if value and not E164_RE.match(value):
        raise _invalid()


# REG-02: Tanzanian mobile numbers (national numbers 6xxxxxxxx / 7xxxxxxxx). Sign-up and
# phone sign-in use this; "0712 345 678", "255712345678" and "+255 712 345 678" are one
# number, so the unique constraint on the stored E.164 value blocks duplicates across formats.
TZ_MOBILE_RE = re.compile(r"^\+255[67][0-9]{8}$")


def normalize_tz_mobile(value):
    """Return a Tanzanian mobile number as +2556XXXXXXXX / +2557XXXXXXXX, or raise."""
    try:
        phone = normalize_phone_number(value)
    except ValidationError:
        phone = None
    if not phone or not TZ_MOBILE_RE.match(phone):
        raise ValidationError(
            _("Enter a Tanzanian mobile number, for example 0712 345 678 or +255 712 345 678."),
            code="invalid_mobile_number",
        )
    return phone


def looks_like_phone(value):
    """True when a sign-in identifier is a phone number rather than an email address."""
    return "@" not in str(value or "")
