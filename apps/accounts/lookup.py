"""REG-02: staff search a member by email (partial) or by mobile number in any format."""

from django.core.exceptions import ValidationError
from django.db.models import Q

from .phone_numbers import normalize_phone_number


def identifier_q(text, prefix="", *, exact_email=False):
    """A Q matching accounts whose email contains ``text`` (or equals it with
    ``exact_email``), or whose stored number is ``text`` normalized (0712..., +255712...)."""
    text = (text or "").strip()
    email_lookup = "email__iexact" if exact_email else "email__icontains"
    condition = Q(**{prefix + email_lookup: text})
    try:
        phone = normalize_phone_number(text)
    except ValidationError:
        phone = None
    if phone:
        condition |= Q(**{prefix + "phone_number": phone})
    return condition
