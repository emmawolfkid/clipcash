from django.conf import settings
from django.core.checks import Error, Tags, Warning, register

THROTTLE_BACKENDS = ("database", "cache")
ENCRYPTED_SSLMODES = ("require", "verify-ca", "verify-full")
LOCAL_DATABASE_HOSTS = ("", "localhost", "127.0.0.1", "::1")


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


@register(Tags.security, deploy=True)
def check_database_tls(app_configs, **kwargs):
    database = settings.DATABASES["default"]
    sslmode = database.get("OPTIONS", {}).get("sslmode", "")
    if database.get("HOST", "") not in LOCAL_DATABASE_HOSTS and sslmode not in ENCRYPTED_SSLMODES:
        return [Warning(
            "The database connection does not require TLS.",
            hint="Set DJANGO_DB_SSLMODE=require (or verify-full), or add ?sslmode=require to DATABASE_URL.",
            id="core.W002",
        )]
    return []


# --- RG-03: unsafe deployment configurations fail `check --deploy --fail-level ERROR` ---
# Each check reads settings only (no database), so the build can run them. They
# report what the repository can determine; hosting facts (who can reach the
# origin, what the web server logs) are verified on the host itself.

PLACEHOLDER_SECRETS = ("replace-with-a-generated-secret-key",)


@register(Tags.security, deploy=True)
def check_debug_off(app_configs, **kwargs):
    if settings.DEBUG:
        return [Error("DEBUG is on in a deployment.",
                      hint="Set DJANGO_DEBUG=False: debug pages expose settings, code and request data.",
                      id="core.E002")]
    return []


@register(Tags.security, deploy=True)
def check_secret_key(app_configs, **kwargs):
    secret = str(settings.SECRET_KEY or "")
    if (len(secret) < 50 or len(set(secret)) < 5 or secret.startswith("django-insecure")
            or secret in PLACEHOLDER_SECRETS):
        return [Error("DJANGO_SECRET_KEY is missing, short, a placeholder or Django's insecure default.",
                      hint='Generate one with python -c "import secrets; print(secrets.token_urlsafe(64))" '
                           "and never reuse the development key.",
                      id="core.E003")]
    return []


@register(Tags.security, deploy=True)
def check_hosts_and_origins(app_configs, **kwargs):
    if settings.DEBUG:
        return []
    messages = []
    hosts = [host.strip() for host in settings.ALLOWED_HOSTS]
    if not hosts or any(host in ("*", ".") for host in hosts):
        messages.append(Error("DJANGO_ALLOWED_HOSTS is empty or accepts every host name.",
                              hint="List the exact host names the site answers to.", id="core.E004"))
    unsafe = [origin for origin in settings.CSRF_TRUSTED_ORIGINS
              if not origin.startswith("https://") or "*" in origin.split("://", 1)[-1].split(".", 1)[-1]]
    if unsafe:
        messages.append(Error(f"{len(unsafe)} DJANGO_CSRF_TRUSTED_ORIGINS entries are not exact HTTPS origins.",
                              hint="Use https://<host> for each origin that may submit forms; no http:// "
                                   "and no wildcard beyond a leading subdomain.",
                              id="core.E005"))
    return messages


@register(Tags.security, deploy=True)
def check_https_redirect(app_configs, **kwargs):
    if not settings.DEBUG and not settings.SECURE_SSL_REDIRECT:
        return [Error("HTTPS is not enforced (SECURE_SSL_REDIRECT is off).",
                      hint="Set DJANGO_SECURE_SSL_REDIRECT=True. Behind a proxy that ends TLS, also set "
                           "DJANGO_SECURE_PROXY_SSL_HEADER=True (Render); with Cloudflare Full (strict) to "
                           "Passenger, leave it False.",
                      id="core.E006")]
    return []


@register(Tags.security, deploy=True)
def check_restricted_demo(app_configs, **kwargs):
    # RG-03 made this an error (core.E007) because leaving the demo opened registration. Since
    # PAY-01, DJANGO_PUBLIC_REGISTRATION_APPROVED is also a runtime gate (apps.core.features):
    # without it registration stays closed whatever the site configuration says. A production
    # deployment that has not yet approved registration (NET-02 Stage 1) is therefore safe and
    # only reported, so the operator knows registration is closed on purpose.
    if not settings.DEBUG and not settings.RESTRICTED_DEMO_MODE and not settings.PUBLIC_REGISTRATION_APPROVED:
        return [Warning("Public registration is closed: DJANGO_PUBLIC_REGISTRATION_APPROVED is not set.",
                        hint="Expected before the reviewed registration launch. Set "
                             "DJANGO_PUBLIC_REGISTRATION_APPROVED=True only after registration acceptance.",
                        id="core.W003")]
    return []


@register(Tags.security, deploy=True)
def check_public_base_url(app_configs, **kwargs):
    """Emailed account links (activation, reset) must point at the site's own HTTPS host.
    Public production requires DJANGO_PUBLIC_BASE_URL; anywhere outside DEBUG a value that
    is set must be an https:// origin whose host is in ALLOWED_HOSTS."""
    if settings.DEBUG:
        return []
    from urllib.parse import urlsplit

    value = settings.PUBLIC_BASE_URL
    hint = ("Set DJANGO_PUBLIC_BASE_URL to the site's HTTPS origin, e.g. https://clipcash.co.tz "
            "(scheme and host only, a host listed in DJANGO_ALLOWED_HOSTS).")
    if not value:
        if settings.RESTRICTED_DEMO_MODE:
            return []  # The demo builds links from the validated request, as before.
        return [Error("DJANGO_PUBLIC_BASE_URL is not set: emailed account links would follow the request.",
                      hint=hint, id="core.E009")]
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    allowed = [entry.strip().lower() for entry in settings.ALLOWED_HOSTS]
    host_allowed = any(host == entry or (entry.startswith(".") and host.endswith(entry)) for entry in allowed)
    if (parts.scheme != "https" or not host or parts.path or parts.query or parts.fragment
            or parts.username or not host_allowed):
        return [Error("DJANGO_PUBLIC_BASE_URL is not an https:// origin for an allowed host.", hint=hint,
                      id="core.E009")]
    return []


@register(Tags.security, deploy=True)
def check_financial_locks(app_configs, **kwargs):
    """Real money stays locked in code (apps.core.features); this trips if that ever changes."""
    from apps.core.features import FINANCIAL_FEATURES, is_enabled

    enabled = [key for key in FINANCIAL_FEATURES if is_enabled(key)]
    if enabled:
        return [Error("Real-money features are enabled: " + ", ".join(sorted(enabled)),
                      hint="Real payments, rewards and withdrawals need a separately reviewed phase.",
                      id="core.E008")]
    return []
