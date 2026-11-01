from django.conf import settings
from django.core.checks import Error, Tags, register

from .throttling import cache_is_shared

ACCOUNT_STATUS_BACKEND = "apps.accounts.backends.AccountStatusBackend"


@register(Tags.security)
def check_throttle_cache(app_configs, **kwargs):
    if settings.ACCOUNTS_THROTTLE_REQUIRE_SHARED_CACHE and not cache_is_shared():
        return [
            Error(
                "Rate limiting requires a cache shared by all processes, but "
                f"CACHES[{settings.ACCOUNTS_THROTTLE_CACHE!r}] is per-process.",
                hint="Set DJANGO_CACHE_URL, e.g. dbcache://clipcash_cache (then run "
                "createcachetable) or a Redis/Memcached URL.",
                id="accounts.E001",
            )
        ]
    return []


@register(Tags.security, deploy=True)
def check_production_security(app_configs, **kwargs):
    """Run by ``manage.py check --deploy`` (part of the deployment procedure)."""
    if settings.DEBUG:
        return []
    errors = []
    for name in ("SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "SESSION_COOKIE_HTTPONLY"):
        if not getattr(settings, name):
            errors.append(
                Error(f"{name} must be True when DEBUG is False.", id="accounts.E002")
            )
    if list(settings.AUTHENTICATION_BACKENDS) != [ACCOUNT_STATUS_BACKEND]:
        errors.append(
            Error(
                "AUTHENTICATION_BACKENDS must contain only the account status backend.",
                id="accounts.E003",
            )
        )
    return errors
