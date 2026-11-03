"""
Central feature flags.

Every flag has an explicit default. Business flags are stored on
SiteConfiguration and can be changed only by Super Administrators (see
apps.core.services). Financial flags have no storage and no override: they are
locked off in code until a later phase adds a reviewed enablement process with
legal, provider and operational sign-off.

Flags marked ``demo_restricted`` are also off while the environment-owned
RESTRICTED_DEMO_MODE setting is on, whatever the stored value.
"""

from dataclasses import dataclass
from functools import wraps

from django.conf import settings
from django.http import Http404
from django.utils.translation import gettext_lazy as _

from .services import get_site_configuration


class UnknownFeatureError(LookupError):
    """A feature name that is not registered (a programming error)."""


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    default: bool
    config_field: str | None = None
    financial: bool = False
    demo_restricted: bool = False

    @property
    def locked(self):
        return self.config_field is None


FEATURES = {
    feature.key: feature
    for feature in (
        Feature("registration", _("Public registration"), True, "registration_enabled", demo_restricted=True),
        Feature("memberships", _("Membership features"), False, "memberships_enabled"),
        Feature("videos", _("Video features"), False, "videos_enabled"),
        Feature("advertisers", _("Advertiser features"), False, "advertisers_enabled"),
        # Locked: no field, no setting, no admin control.
        Feature("real_money_rewards", _("Real-money rewards"), False, financial=True),
        Feature("live_payments", _("Live payments"), False, financial=True),
        Feature("withdrawals", _("Withdrawals and payouts"), False, financial=True),
    )
}

FINANCIAL_FEATURES = frozenset(key for key, feature in FEATURES.items() if feature.financial)


def get_feature(key):
    try:
        return FEATURES[key]
    except KeyError:
        raise UnknownFeatureError(f"Unknown feature flag: {key!r}") from None


def is_enabled(key, config=None):
    feature = get_feature(key)
    if feature.financial or feature.locked:
        return False
    if feature.demo_restricted and settings.RESTRICTED_DEMO_MODE:
        return False  # Before the configuration lookup: no stored value can override it.
    config = config if config is not None else get_site_configuration()
    value = getattr(config, feature.config_field, feature.default)
    return value if isinstance(value, bool) else feature.default


def feature_states(config=None):
    config = config if config is not None else get_site_configuration()
    return {key: is_enabled(key, config) for key in FEATURES}


def feature_required(key):
    """View decorator: 404 when the feature is disabled."""
    get_feature(key)  # Fail at import time for typos.

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not is_enabled(key):
                raise Http404
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator
