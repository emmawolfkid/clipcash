"""
Registered access entitlements a plan can grant.

Video playback (Phase 04) and viewing verification (Phase 06) ask
apps.memberships.access.check_access(user, entitlement=...) for one of these
codes. Plans may only list registered codes.
"""

from django.utils.translation import gettext_lazy as _

ENTITLEMENTS = {
    "videos.standard": _("Standard video library"),
    "videos.premium": _("Premium video library"),
    "videos.hd": _("High-definition playback"),
}


def label(code):
    return ENTITLEMENTS.get(code, code)
