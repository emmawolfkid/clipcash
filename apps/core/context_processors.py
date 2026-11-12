from django.conf import settings

from .features import FINANCIAL_FEATURES, feature_states
from .services import get_site_configuration


def site(request):
    config = getattr(request, "site_configuration", None) or get_site_configuration()
    from apps.accounts.access import demo_unconfirmed, demo_verification_optional
    from apps.payments.providers import sandbox_active

    user = getattr(request, "user", None)
    states = feature_states(config)
    return {"site_config": config, "features": states, "sandbox_payments": sandbox_active(),
            "demo_verification_optional": demo_verification_optional(),
            "demo_unconfirmed_account": user is not None and demo_unconfirmed(user),
            # REG-01: public pages describe the environment they run in. Demo disclosures
            # show only in the restricted demonstration; payments, cash rewards and
            # withdrawals are described as available only when their feature is on.
            "restricted_demo": settings.RESTRICTED_DEMO_MODE,
            "financial_locked": not any(states[key] for key in FINANCIAL_FEATURES)}
