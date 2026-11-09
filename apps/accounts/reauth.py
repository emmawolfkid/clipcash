"""
Recent password re-verification for sensitive account actions (RG-03).

Policy: an action that needs it (completing an email-address change) is allowed
without asking for the password again only if THIS session confirmed the current
password of THIS account within settings.ACCOUNTS_REAUTHENTICATION_SECONDS
(5 minutes). A password confirmed on the change request or on the confirmation
page counts; signing in does not. Otherwise the page asks for the password.

The mark lives in the session, so it ends with the session (sign-out, expiry,
or the session being revoked by a password or address change).
"""

from django.conf import settings
from django.utils import timezone

SESSION_KEY = "_clipcash_reauthenticated"


def mark(request, user):
    request.session[SESSION_KEY] = {"user": str(user.pk), "at": int(timezone.now().timestamp())}


def is_fresh(request, user):
    value = request.session.get(SESSION_KEY)
    if not isinstance(value, dict) or value.get("user") != str(user.pk):
        return False
    try:
        age = timezone.now().timestamp() - int(value["at"])
    except (KeyError, TypeError, ValueError):
        return False
    return 0 <= age <= settings.ACCOUNTS_REAUTHENTICATION_SECONDS


def forget(request):
    request.session.pop(SESSION_KEY, None)
