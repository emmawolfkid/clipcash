"""Presentation helpers for backend-confirmed action messages."""
from django import template

register = template.Library()


@register.filter
def unique_feedback(messages):
    """Consume Django's storage normally, preserving order and severity."""
    positions = {}
    unique = []
    for message in messages:
        key = (message.level, str(message))
        if key not in positions:
            positions[key] = len(unique)
            unique.append(message)
        elif "persistent" in (message.extra_tags or "").split():
            unique[positions[key]] = message
    return unique
