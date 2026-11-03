from .features import feature_states
from .services import get_site_configuration


def site(request):
    config = getattr(request, "site_configuration", None) or get_site_configuration()
    from apps.accounts.access import demo_unconfirmed, demo_verification_optional
    from apps.payments.providers import sandbox_active

    user = getattr(request, "user", None)
    return {"site_config": config, "features": feature_states(config), "sandbox_payments": sandbox_active(),
            "demo_verification_optional": demo_verification_optional(),
            "demo_unconfirmed_account": user is not None and demo_unconfirmed(user)}
