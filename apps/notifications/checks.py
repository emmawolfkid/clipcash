from django.conf import settings
from django.core.checks import Error, Warning, register

NON_DELIVERING_BACKENDS = ("console", "locmem", "dummy", "filebased")
# RG-03: these keep every message, so activation, reset and email-change links would sit
# in logs or files readable by whoever can read those. Refused in deployments.
RECORDING_BACKENDS = ("console", "locmem", "filebased")
LOCAL_SMTP_HOSTS = ("localhost", "127.0.0.1", "::1")


@register(deploy=True)
def check_email_delivery(app_configs, **kwargs):
    """Account activation, confirmation and reset depend on real delivery (audit CC-003)."""
    if settings.DEBUG:
        return []
    backend = settings.EMAIL_BACKEND
    if any(name in backend for name in RECORDING_BACKENDS):
        return [Error(
            f"EMAIL_BACKEND {backend!r} records every message, including single-use account links.",
            hint="Use the SMTP backend, or django.core.mail.backends.dummy.EmailBackend with the documented "
                 "restricted-demo waiver (DJANGO_DEMO_EMAIL_VERIFICATION_OPTIONAL and DJANGO_DEMO_ACCOUNTS).",
            id="notifications.E001",
        )]
    messages = []
    if any(name in backend for name in NON_DELIVERING_BACKENDS):
        messages.append(Warning(
            f"EMAIL_BACKEND {backend!r} does not deliver email: nobody can activate an account, "
            "confirm an email address or reset a password.",
            hint="Set DJANGO_EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend and the DJANGO_EMAIL_* "
                 "values on the web service and the worker, then run manage.py email_diagnostics --connect.",
            id="notifications.W001",
        ))
    if "smtp" in backend:
        if settings.EMAIL_USE_TLS and settings.EMAIL_USE_SSL:
            messages.append(Error("DJANGO_EMAIL_USE_TLS and DJANGO_EMAIL_USE_SSL are both on.",
                                  hint="Use TLS (STARTTLS, usually port 587) or SSL (port 465), not both.",
                                  id="notifications.E002"))
        elif (not settings.EMAIL_USE_TLS and not settings.EMAIL_USE_SSL
              and settings.EMAIL_HOST.strip().lower() not in LOCAL_SMTP_HOSTS):
            messages.append(Error("SMTP to a remote server is configured without TLS or SSL.",
                                  hint="The password and every account link would cross the network unencrypted. "
                                       "Set DJANGO_EMAIL_USE_TLS=True (port 587) or DJANGO_EMAIL_USE_SSL=True (465).",
                                  id="notifications.E003"))
    return messages
