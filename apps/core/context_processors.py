from .features import feature_states
from .services import get_site_configuration


def site(request):
    config = getattr(request, "site_configuration", None) or get_site_configuration()
    from apps.payments.providers import sandbox_active

    return {"site_config": config, "features": feature_states(config), "sandbox_payments": sandbox_active()}
