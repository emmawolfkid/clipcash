"""Phase 14-B (B4, audit CC-003): email delivery readiness tools."""

from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from apps.accounts.tests.helpers import run_worker
from apps.notifications.checks import check_email_delivery
from apps.notifications.models import OutboundEmail
from apps.notifications.services import TEST_EMAIL

SMTP = "django.core.mail.backends.smtp.EmailBackend"


class EmailDiagnosticsTests(TestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command("email_diagnostics", *args, stdout=out)
        return out.getvalue()

    @override_settings(EMAIL_HOST_PASSWORD="not-shown-anywhere", EMAIL_HOST_USER="mailer")
    def test_shows_configuration_without_secrets_and_sends_nothing(self):
        output = self.run_command()
        self.assertIn("does NOT deliver email", output)  # Tests use the locmem backend.
        self.assertIn("password set: yes", output)
        self.assertNotIn("not-shown-anywhere", output)
        self.assertFalse(OutboundEmail.objects.exists())

    def test_connect_opens_and_closes_without_sending(self):
        connection = mock.MagicMock()
        with mock.patch("apps.notifications.management.commands.email_diagnostics.get_connection",
                        return_value=connection):
            self.assertIn("No email was sent", self.run_command("--connect"))
        connection.open.assert_called_once()
        connection.close.assert_called_once()
        connection.send_messages.assert_not_called()

    def test_connect_failure_reports_only_the_error_class(self):
        connection = mock.MagicMock()
        connection.open.side_effect = OSError("smtp.secret-host.example refused: user=admin")
        with mock.patch("apps.notifications.management.commands.email_diagnostics.get_connection",
                        return_value=connection), self.assertRaises(CommandError) as ctx:
            self.run_command("--connect")
        self.assertEqual(str(ctx.exception), "Connection failed (OSError).")

    def test_send_to_queues_one_test_email_through_the_outbox(self):
        from django.core import mail

        self.assertIn("Queued test email", self.run_command("--send-to", "ops@example.com"))
        email = OutboundEmail.objects.get()
        self.assertEqual((email.kind, email.recipient, email.context), (TEST_EMAIL, "ops@example.com", {}))
        run_worker()
        email.refresh_from_db()
        self.assertEqual(email.status, OutboundEmail.Status.SENT)
        self.assertEqual(mail.outbox[-1].subject, "ClipCash email delivery test")
        self.assertNotIn("http", mail.outbox[-1].body)  # No links or tokens.

    def test_send_to_rejects_an_invalid_address(self):
        with self.assertRaises(CommandError):
            self.run_command("--send-to", "not-an-address")
        self.assertFalse(OutboundEmail.objects.exists())


class EmailDeployCheckTests(TestCase):
    @override_settings(DEBUG=False, EMAIL_BACKEND="django.core.mail.backends.dummy.EmailBackend")
    def test_non_delivering_backend_warns_in_production(self):
        self.assertEqual([m.id for m in check_email_delivery(None)], ["notifications.W001"])

    def test_backends_that_record_account_links_fail_in_production(self):
        """RG-03: console, locmem and file backends would keep every token link."""
        for name in ("console", "locmem", "filebased"):
            with self.subTest(name), override_settings(DEBUG=False,
                                                       EMAIL_BACKEND=f"django.core.mail.backends.{name}.EmailBackend"):
                self.assertEqual([m.id for m in check_email_delivery(None)], ["notifications.E001"])

    @override_settings(DEBUG=True, EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend")
    def test_console_is_fine_in_development(self):
        self.assertEqual(check_email_delivery(None), [])

    @override_settings(DEBUG=False, EMAIL_BACKEND=SMTP)
    def test_smtp_backend_passes(self):
        self.assertEqual(check_email_delivery(None), [])

    @override_settings(DEBUG=False, EMAIL_BACKEND=SMTP, EMAIL_HOST="smtp.example.com", EMAIL_USE_TLS=False,
                       EMAIL_USE_SSL=False)
    def test_unencrypted_remote_smtp_fails(self):
        self.assertEqual([m.id for m in check_email_delivery(None)], ["notifications.E003"])
        with override_settings(EMAIL_USE_TLS=True):
            self.assertEqual(check_email_delivery(None), [])
        with override_settings(EMAIL_USE_SSL=True):
            self.assertEqual(check_email_delivery(None), [])
        with override_settings(EMAIL_USE_TLS=True, EMAIL_USE_SSL=True):
            self.assertEqual([m.id for m in check_email_delivery(None)], ["notifications.E002"])
