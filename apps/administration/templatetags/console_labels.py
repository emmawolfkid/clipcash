from django import template

from ..labels import code_label as _code_label

register = template.Library()


@register.filter
def code_label(code):
    """``{{ session.outcome|code_label }}``: readable text for an internal code. Display only."""
    return _code_label(code)


@register.filter
def grouped(value):
    """``{{ 75000000|grouped }}`` -> ``75,000,000``. Text such as ``100%`` passes through unchanged."""
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else value


@register.filter
def code_labels(codes):
    """``{{ session.flags|code_labels }}``: a list of internal codes as readable text."""
    return ", ".join(str(_code_label(code)) for code in codes or [])
