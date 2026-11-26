from django import template
from django.utils.translation import ngettext

from ..money import MoneyError, format_amount

register = template.Library()


@register.filter
def tzs(amount):
    """``{{ 1500|tzs }}`` -> ``TZS 1,500``. Invalid values render as an empty string."""
    try:
        return format_amount(amount)
    except MoneyError:
        return ""


@register.filter
def policy_amount(policy):
    """FIN-01: a reward policy's amount in its own unit: ``TZS 100`` (real) or ``100 credits`` (non-cash)."""
    if getattr(policy, "kind", "CREDITS") == "TZS":
        return tzs(policy.reward_amount)
    return credits(policy.reward_amount)


@register.filter
def credits(amount):
    """CR-01: ``{{ 1500|credits }}`` -> ``1,500 credits`` (non-cash campaign credits, never money)."""
    try:
        value = int(amount)
    except (TypeError, ValueError):
        return ""
    return ngettext("%(amount)s credit", "%(amount)s credits", value) % {"amount": f"{value:,}"}
