"""Presentation helpers for backend-confirmed action messages (components/messages.html)."""
from django import template

register = template.Library()

# "feedback-persist" is the tag views use; "persistent" is the UI-09 spelling, kept as an alias.
PERSISTENT_TAGS = frozenset({"feedback-persist", "persistent"})


def is_persistent(message):
    return not PERSISTENT_TAGS.isdisjoint((message.extra_tags or "").split())


@register.filter
def unique_feedback(messages):
    """Consume Django's storage normally, preserving order and severity. A repeated message
    (same level and text) is shown once; if any copy is persistent, the persistent one is kept."""
    positions = {}
    unique = []
    for message in messages:
        key = (message.level, str(message))
        if key not in positions:
            positions[key] = len(unique)
            unique.append(message)
        elif is_persistent(message):
            unique[positions[key]] = message
    return unique


@register.filter
def feedback_auto(message):
    """True when the message may dismiss itself: a success that is not explicitly persistent.
    The script also keeps any card that holds a link or form control."""
    return message.level_tag == "success" and not is_persistent(message)
