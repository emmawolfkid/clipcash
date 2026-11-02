from django.conf import settings
from django.core.checks import Error, register


@register()
def check_payments_mode(app_configs, **kwargs):
    if settings.PAYMENTS_MODE not in ("sandbox", "disabled"):
        return [Error("DJANGO_PAYMENTS_MODE must be 'sandbox' or 'disabled'. Live payments do not exist yet.",
                      id="payments.E001")]
    return []


@register(deploy=True)
def check_sandbox_in_production(app_configs, **kwargs):
    # Deploy-only (the test runner forces DEBUG=False). At runtime sandbox_active()
    # refuses sandbox payments outside DEBUG anyway.
    if settings.PAYMENTS_MODE == "sandbox" and not (settings.DEBUG or settings.PAYMENTS_SANDBOX_ALLOWED):
        return [Error("Sandbox payments are refused in production.",
                      hint="Use DJANGO_PAYMENTS_MODE=disabled, or DJANGO_PAYMENTS_SANDBOX_ALLOWED=True only on an "
                           "isolated demo/staging deployment.", id="payments.E002")]
    return []
