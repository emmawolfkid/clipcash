from django import template

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
def credits(amount):
    """CR-01: ``{{ 1500|credits }}`` -> ``1,500 credits`` (non-cash campaign credits, never money)."""
    try:
        value = int(amount)
    except (TypeError, ValueError):
        return ""
    return f"{value:,} credit" + ("" if value == 1 else "s")
