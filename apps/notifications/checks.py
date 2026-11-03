from django.conf import settings
from django.core.checks import Warning, register

NON_DELIVERING_BACKENDS = ("console", "locmem", "dummy", "filebased")


@register(deploy=True)
def check_email_delivery(app_configs, **kwargs):
    """Account activation, confirmation and reset depend on real delivery (audit CC-003)."""
    if settings.DEBUG or not any(name in settings.EMAIL_BACKEND for name in NON_DELIVERING_BACKENDS):
        return []
    return [Warning(
        f"EMAIL_BACKEND {settings.EMAIL_BACKEND!r} does not deliver email: nobody can activate an account, "
        "confirm an email address or reset a password.",
        hint="Set DJANGO_EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend and the DJANGO_EMAIL_* "
             "values on the web service and the worker, then run manage.py email_diagnostics --connect.",
        id="notifications.W001",
    )]
