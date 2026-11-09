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
    messages = _check_proxy_boundary(proxies)
    if settings.DEPLOYED_ON_RENDER and proxies == 1:
        messages.append(Warning(
            "DJANGO_TRUSTED_PROXY_COUNT is 1 on Render, where requests pass Cloudflare and Render's load balancer.",
            hint="With 1, every visitor resolves to a Cloudflare edge address. Use 2 unless the Operational "
                 "health page shows the forwarded entries differ.",
            id="accounts.W002",
        ))
    return messages


def _check_proxy_boundary(proxies):
    """RG-03: forwarded headers are believed only from peers known to be the proxy."""
    import ipaddress

    from .throttling import CLIENT_IP_HEADERS

    messages = []
    header = settings.ACCOUNTS_CLIENT_IP_HEADER
    networks = settings.ACCOUNTS_TRUSTED_PROXY_NETWORKS
    invalid = []
    for value in networks:
        try:
            ipaddress.ip_network(value.strip(), strict=False)
        except ValueError:
            invalid.append(value)
    if invalid:
        messages.append(Error(
            f"DJANGO_TRUSTED_PROXY_NETWORKS has {len(invalid)} invalid network(s).",
            hint="Use CIDR ranges such as 173.245.48.0/20 or 2400:cb00::/32, separated by commas.",
            id="accounts.E006",
        ))
    if header and header not in CLIENT_IP_HEADERS:
        messages.append(Error(
            f"DJANGO_CLIENT_IP_HEADER {header!r} is not supported.",
            hint="Use one of: " + ", ".join(sorted(CLIENT_IP_HEADERS)) + ", or leave it empty.",
            id="accounts.E008",
        ))
    if header and not networks:
        messages.append(Error(
            "DJANGO_CLIENT_IP_HEADER is set without DJANGO_TRUSTED_PROXY_NETWORKS.",
            hint="Any client can send that header. List the proxy's networks (e.g. Cloudflare's ranges) so the "
                 "header is believed only from them.",
            id="accounts.E005",
        ))
    elif (proxies > 0 or header) and not networks and not settings.DEPLOYED_ON_RENDER \
            and not settings.ACCOUNTS_PROXY_ORIGIN_RESTRICTED:
        messages.append(Error(
            "Forwarded client addresses are trusted from any peer.",
            hint="If the server can be reached without the proxy, a client can forge X-Forwarded-For to evade "
                 "per-address limits and falsify audit records. Set DJANGO_TRUSTED_PROXY_NETWORKS to the proxy's "
                 "networks, or, only if the web server itself refuses every other peer, "
                 "DJANGO_PROXY_ORIGIN_RESTRICTED=True. See docs/deployment/netpoa_runbook.md (client addresses).",
            id="accounts.E007",
        ))
    if networks and proxies <= 0 and not header:
        messages.append(Warning(
            "DJANGO_TRUSTED_PROXY_NETWORKS is set but no proxy count or client-address header is configured.",
            hint="The networks have no effect: set DJANGO_TRUSTED_PROXY_COUNT or DJANGO_CLIENT_IP_HEADER.",
            id="accounts.W003",
        ))
    return messages
