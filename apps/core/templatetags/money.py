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
