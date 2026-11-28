"""OPS-01: how an account is shown in page headers: the person's name (or a safe fallback), initials and a
short role label. Never the internal ID, email address, phone number or username."""

from django import template
from django.utils.translation import gettext as _

register = template.Library()


@register.filter
def display_name(user):
    first = (getattr(user, "first_name", "") or "").strip()
    last = (getattr(user, "last_name", "") or "").strip()
    name = f"{first} {last}".strip()
    return name or _("My account")


@register.filter
def initials(user):
    parts = [(getattr(user, "first_name", "") or "").strip(), (getattr(user, "last_name", "") or "").strip()]
    letters = "".join(part[0] for part in parts if part)
    return letters.upper()[:2]


@register.filter
def role_label(user):
    role = getattr(user, "role", "")
    if role == "SUPER_ADMIN":
        return _("Administrator")
    if getattr(user, "is_staff", False):
        return _("Staff")
    memberships = getattr(user, "advertiser_memberships", None)
    if getattr(user, "pk", None) and memberships is not None and memberships.exists():
        return _("Member · Advertiser")
    return _("Member")
