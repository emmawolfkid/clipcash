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
