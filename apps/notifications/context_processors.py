from django.utils.functional import SimpleLazyObject

from . import inbox


def notifications(request):
    """Unread count for the header bell; queried only when a template uses it."""
    user = getattr(request, "user", None)
    return {"unread_notifications": SimpleLazyObject(lambda: inbox.unread_count(user))}
