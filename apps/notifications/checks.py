from email.utils import parseaddr

from django.conf import settings
from django.core.checks import Error, Warning, register

NON_DELIVERING_BACKENDS = ("console", "locmem", "dummy", "filebased")
# RG-03: these keep every message, so activation, reset and email-change links would sit
# in logs or files readable by whoever can read those. Refused in deployments.
RECORDING_BACKENDS = ("console", "locmem", "filebased")
LOCAL_SMTP_HOSTS = ("localhost", "127.0.0.1", "::1")
# PRD-01: senders that no real mailbox provider will accept or deliver from.
PLACEHOLDER_SENDER_DOMAINS = ("localhost", "localhost.localdomain", "example.com", "example.org", "example.net")


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
    delivery_hint = ("Set DJANGO_EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend and the DJANGO_EMAIL_* "
                     "values on the web service and the worker, then run manage.py email_diagnostics --connect.")
    if any(name in backend for name in NON_DELIVERING_BACKENDS):
        if settings.RESTRICTED_DEMO_MODE:
            messages.append(Warning(
                f"EMAIL_BACKEND {backend!r} does not deliver email: nobody can activate an account, "
                "confirm an email address or reset a password.", hint=delivery_hint, id="notifications.W001"))
        else:
            # PRD-01: in public production an email sign-up is activated only by its emailed link,
            # and password recovery is by email only, so a non-delivering backend is an error.
            messages.append(Error(
                f"EMAIL_BACKEND {backend!r} does not deliver email, but public production needs it: email "
                "sign-ups are activated and passwords are reset only through emailed links.",
                hint=delivery_hint, id="notifications.E004"))
    if not settings.RESTRICTED_DEMO_MODE:
        sender = parseaddr(settings.DEFAULT_FROM_EMAIL)[1].lower()
        domain = sender.rpartition("@")[2]
        if "@" not in sender or domain in PLACEHOLDER_SENDER_DOMAINS or "." not in domain:
            messages.append(Error(
                "DJANGO_DEFAULT_FROM_EMAIL is not a real sender address.",
                hint="Use an address on the site's own domain that the SMTP provider is allowed to send from "
                     "(SPF/DKIM set up), e.g. ClipCash <no-reply@clipcash.co.tz>.", id="notifications.E005"))
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
