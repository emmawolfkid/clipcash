from django.conf import settings
from django.core.checks import Error, Tags, Warning, register

THROTTLE_BACKENDS = ("database", "cache")


@register(Tags.security)
def check_throttle_backend(app_configs, **kwargs):
    if settings.ACCOUNTS_THROTTLE_BACKEND not in THROTTLE_BACKENDS:
        return [Error(f"ACCOUNTS_THROTTLE_BACKEND must be one of {THROTTLE_BACKENDS}.", id="core.E001")]
    return []


@register(Tags.security, deploy=True)
def check_production_throttle_backend(app_configs, **kwargs):
    if not settings.DEBUG and settings.ACCOUNTS_THROTTLE_BACKEND != "database":
        return [Warning(
            "Rate limits use the cache backend in production.",
            hint="Set DJANGO_THROTTLE_BACKEND=database: Django's database cache cannot increment "
                 "atomically, so concurrent requests can undercount attempts.",
            id="core.W001",
        )]
    return []
