from django.conf import settings
from django.core.checks import Error, Tags, Warning, register

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
    if settings.DEMO_EMAIL_VERIFICATION_OPTIONAL and not settings.RESTRICTED_DEMO_MODE:
        errors.append(
            Warning(
                "DJANGO_DEMO_EMAIL_VERIFICATION_OPTIONAL is set without restricted demo mode; it has no effect.",
                hint="Unset it: email verification is required outside the restricted demonstration.",
                id="accounts.W001",
            )
        )
    if list(settings.AUTHENTICATION_BACKENDS) != [ACCOUNT_STATUS_BACKEND]:
        errors.append(
            Error(
                "AUTHENTICATION_BACKENDS must contain only the account status backend.",
                id="accounts.E003",
            )
        )
    errors += _check_client_ip_configuration()
    return errors


def _check_client_ip_configuration():
    """Behind a reverse proxy, REMOTE_ADDR is the proxy's address. Without a trusted
    proxy count every visitor would share one address, so per-IP rate limits would
    become site-wide and audit records would show the proxy (audit CC-004)."""
    behind_proxy = bool(getattr(settings, "SECURE_PROXY_SSL_HEADER", None)) or settings.DEPLOYED_ON_RENDER
    proxies = settings.ACCOUNTS_TRUSTED_PROXY_COUNT
    if behind_proxy and proxies <= 0:
        return [Error(
            "The site runs behind a reverse proxy but DJANGO_TRUSTED_PROXY_COUNT is 0.",
            hint="Set it to the number of proxies that append to X-Forwarded-For (on Render, Cloudflare and "
                 "Render's load balancer: 2) and verify it on the Operational health page. See "
                 "docs/deployment/render.md (client IP).",
            id="accounts.E004",
        )]
    if settings.DEPLOYED_ON_RENDER and proxies == 1:
        return [Warning(
            "DJANGO_TRUSTED_PROXY_COUNT is 1 on Render, where requests pass Cloudflare and Render's load balancer.",
            hint="With 1, every visitor resolves to a Cloudflare edge address. Use 2 unless the Operational "
                 "health page shows the forwarded entries differ.",
            id="accounts.W002",
        )]
    return []
