"""
Check email delivery readiness (audit CC-003). Read-only unless asked:

    python manage.py email_diagnostics                    # show configuration (no secrets)
    python manage.py email_diagnostics --connect          # open and close an SMTP connection; sends nothing
    python manage.py email_diagnostics --send-to ops@example.com
                                                          # queue ONE test email in the outbox; the
                                                          # background worker delivers it (run_jobs)

The test email goes through the same outbox, job and SMTP path as account emails,
so its arrival proves the worker and the email provider work end to end.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import get_connection
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email

from apps.audit.context import audit_scope
from apps.notifications.services import TEST_EMAIL, enqueue_email

NON_DELIVERING = ("console", "locmem", "dummy", "filebased")


class Command(BaseCommand):
    help = "Show email configuration, optionally test the SMTP connection or queue one test email."

    def add_arguments(self, parser):
        parser.add_argument("--connect", action="store_true", help="Open and close an SMTP connection.")
        parser.add_argument("--send-to", default="", help="Queue one test email to this address.")

    def handle(self, *args, connect, send_to, **options):
        backend = settings.EMAIL_BACKEND
        delivering = not any(name in backend for name in NON_DELIVERING)
        out = self.stdout.write
        out(f"Backend:        {backend}{'' if delivering else '  (does NOT deliver email)'}")
        out(f"Host / port:    {settings.EMAIL_HOST}:{settings.EMAIL_PORT}")
        out(f"TLS / SSL:      {settings.EMAIL_USE_TLS} / {settings.EMAIL_USE_SSL}")
        out(f"Login user set: {'yes' if settings.EMAIL_HOST_USER else 'no'}; "
            f"password set: {'yes' if settings.EMAIL_HOST_PASSWORD else 'no'}")
        out(f"From:           {settings.DEFAULT_FROM_EMAIL}")
        out(f"Timeout:        {settings.EMAIL_TIMEOUT} s")
        if connect:
            try:
                connection = get_connection(fail_silently=False)
                connection.open()
                connection.close()
            except Exception as error:  # noqa: BLE001 - report the class only; server text can echo secrets.
                raise CommandError(f"Connection failed ({type(error).__name__}).") from None
            out(self.style.SUCCESS("Connection opened and closed. No email was sent."))
        if send_to:
            try:
                validate_email(send_to)
            except ValidationError:
                raise CommandError("--send-to must be an email address.") from None
            with audit_scope(source="CLI"):
                email = enqueue_email(TEST_EMAIL, recipient=send_to, context={})
            out(self.style.SUCCESS(f"Queued test email {email.pk}. The worker (run_jobs) delivers it; check the "
                                   "inbox and /admin/ -> Outbound emails for its status."))
