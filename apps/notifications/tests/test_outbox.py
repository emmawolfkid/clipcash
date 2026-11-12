import json
import re
import smtplib
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Permission
from django.core import mail
from django.db import transaction
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts import services as account_services
from apps.accounts.tests.helpers import AccountsTestCase, User, make_user, run_worker
from apps.accounts.tokens import verification_token_generator
from apps.audit.models import AuditEvent
from apps.core.models import Job
from apps.notifications.models import OutboundEmail

S = OutboundEmail.Status
SMTP_SECRET_MARKER = "SMTP_SERVER_TEXT_Qz7Kx"


def register(email):
    """A PENDING email account and its activation email, in one transaction (as the
    pre-REG-02 sign-up did; REG-02 sign-up is phone-first and sends no email)."""
    with transaction.atomic():
        user = User.objects.create_user(email, None, status=User.Status.PENDING)
        account_services.send_verification_email(user, request())
    return user


def request():
    return RequestFactory().post("/accounts/register/", HTTP_HOST="testserver")


def token_from(body):
    return re.search(r"/accounts/(?:activate|confirm-email)/[^/]+/([^/\s]+)/", body).group(1)


def everything_stored():
    """Every column of every outbox, job and audit row, as text."""
    rows = list(OutboundEmail.objects.values()) + list(Job.objects.values()) + list(AuditEvent.objects.values())
    return json.dumps(rows, default=str)


class TransactionalEnqueueTests(AccountsTestCase):
    def test_rolled_back_registration_sends_nothing(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            register("new@example.com")
            raise RuntimeError("later step failed")
        run_worker()
        self.assertFalse(User.objects.exists())
        self.assertFalse(OutboundEmail.objects.exists())
        self.assertFalse(Job.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_registration_queues_one_email_and_one_job(self):
        register("new@example.com")
        email = OutboundEmail.objects.get()
        self.assertEqual((email.kind, email.status, email.recipient), ("accounts.activation", S.PENDING, "new@example.com"))
        self.assertEqual(Job.objects.get().payload, {"email_id": str(email.pk)})
        self.assertEqual(mail.outbox, [])  # Nothing is sent inside the request.
        run_worker()
        email.refresh_from_db()
        self.assertEqual((email.status, email.attempts), (S.SENT, 1))
        self.assertEqual(len(mail.outbox), 1)


class TokenConfidentialityTests(AccountsTestCase):
    def test_tokens_exist_only_in_the_delivered_email(self):
        register("new@example.com")
        stored_before = everything_stored()
        run_worker()
        token = token_from(mail.outbox[0].body)
        self.assertNotIn(token, stored_before)
        self.assertNotIn(token, everything_stored())
        self.assertEqual(OutboundEmail.objects.get().context, {"base_url": "http://testserver"})

    def test_admin_listing_shows_no_content(self):
        register("new@example.com")
        run_worker()
        token = token_from(mail.outbox[0].body)
        root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.client.force_login(root)
        email = OutboundEmail.objects.get()
        for url in (reverse("admin:notifications_outboundemail_changelist"),
                    reverse("admin:notifications_outboundemail_change", args=[email.pk]),
                    reverse("admin:core_job_changelist")):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, token)
                self.assertNotContains(response, "new@example.com")
        self.assertContains(self.client.get(reverse("admin:notifications_outboundemail_changelist")),
                            "n***@example.com")


class ExpiryTests(AccountsTestCase):
    def test_delayed_delivery_does_not_extend_token_validity(self):
        register("new@example.com")
        two_hours_ago = timezone.now() - timedelta(hours=2)
        OutboundEmail.objects.update(token_issued_at=two_hours_ago)
        run_worker()  # Delivered two hours "late".
        user = User.objects.get()
        token = token_from(mail.outbox[0].body)
        self.assertTrue(verification_token_generator.check_token(user, token))
        with override_settings(ACCOUNTS_EMAIL_VERIFICATION_TIMEOUT=3600):
            # The link's age counts from the request, not from delivery.
            self.assertFalse(verification_token_generator.check_token(user, token))

    def test_expired_message_is_not_sent(self):
        register("new@example.com")
        OutboundEmail.objects.update(token_issued_at=timezone.now() - timedelta(days=2),
                                     expires_at=timezone.now() - timedelta(seconds=1))
        run_worker()
        self.assertEqual(OutboundEmail.objects.get().status, S.EXPIRED)
        self.assertEqual(mail.outbox, [])

    def test_stale_messages_are_skipped(self):
        register("first@example.com")
        register("second@example.com")
        first = User.objects.get(email="first@example.com")
        first.status = User.Status.DISABLED
        first.save()
        second = User.objects.get(email="second@example.com")
        second.email = "changed@example.com"
        second.save()
        run_worker()
        reasons = dict(OutboundEmail.objects.values_list("recipient", "skip_reason"))
        self.assertEqual(reasons, {"first@example.com": "status_changed", "second@example.com": "address_changed"})
        self.assertEqual(mail.outbox, [])


class DeliveryFailureTests(AccountsTestCase):
    def queue(self):
        register("new@example.com")
        return OutboundEmail.objects.get(), Job.objects.get()

    def retry_now(self):
        Job.objects.filter(status=Job.Status.RETRY_WAIT).update(run_after=timezone.now())

    def test_transient_smtp_failure_retries_then_fails_terminally(self):
        email, job = self.queue()
        # The server's text must never be stored. The marker contains characters (upper case,
        # "_", letters after "f") that cannot occur in the hex ids, UUIDs or timestamps that
        # are legitimately stored, so finding it can only mean the message leaked.
        error = smtplib.SMTPServerDisconnected(f"connection to smtp.example.com lost; token={SMTP_SECRET_MARKER}")
        with mock.patch("apps.notifications.services.send_mail", side_effect=error):
            run_worker()
            job.refresh_from_db()
            email.refresh_from_db()
            self.assertEqual((job.status, job.last_error_class, email.status),
                             (Job.Status.RETRY_WAIT, "TRANSIENT", S.PENDING))
            self.assertNotIn("smtp.example.com", job.last_error)
            for _ in range(job.max_attempts - 1):
                self.retry_now()
                run_worker()
        job.refresh_from_db()
        email.refresh_from_db()
        self.assertEqual((job.status, email.status, email.attempts), (Job.Status.FAILED, S.FAILED, job.max_attempts))
        event = AuditEvent.objects.get(action="notifications.email.failed")
        self.assertEqual(event.metadata, {"kind": "accounts.activation", "error_class": "TRANSIENT",
                                          "attempts": job.max_attempts})
        self.assertTrue(AuditEvent.objects.filter(action="jobs.job.failed").exists())
        stored = everything_stored()
        self.assertNotIn(SMTP_SECRET_MARKER, stored)
        self.assertNotIn("smtp.example.com", stored)
        # Account state is untouched by delivery failures.
        self.assertEqual(User.objects.get().status, User.Status.PENDING)

    def test_permanent_smtp_rejection_fails_immediately(self):
        email, job = self.queue()
        refused = smtplib.SMTPRecipientsRefused({"new@example.com": (550, b"no such user")})
        with mock.patch("apps.notifications.services.send_mail", side_effect=refused):
            run_worker()
        job.refresh_from_db()
        email.refresh_from_db()
        self.assertEqual((job.status, job.attempts, email.status), (Job.Status.FAILED, 1, S.FAILED))

    def test_rerun_after_success_does_not_send_twice(self):
        email, job = self.queue()
        run_worker()
        Job.objects.filter(pk=job.pk).update(status=Job.Status.PENDING, attempts=0)  # Simulated duplicate run.
        run_worker()
        self.assertEqual(len(mail.outbox), 1)

    def test_retry_of_failed_delivery_cannot_resend_expired_link(self):
        from apps.core.jobs.services import retry_job

        email, job = self.queue()
        with mock.patch("apps.notifications.services.send_mail", side_effect=smtplib.SMTPRecipientsRefused({})):
            run_worker()
        OutboundEmail.objects.update(status=S.PENDING, expires_at=timezone.now() - timedelta(seconds=1),
                                     token_issued_at=timezone.now() - timedelta(days=2))
        retry_job(Job.objects.get(pk=job.pk), actor=make_user("root@example.com", role=User.Role.SUPER_ADMIN))
        run_worker()
        self.assertEqual(OutboundEmail.objects.get().status, S.EXPIRED)
        self.assertEqual(mail.outbox, [])


class OutboxAccessTests(AccountsTestCase):
    def test_outbox_admin_requires_operations_permission(self):
        cm = make_user("cm@example.com", role=User.Role.CONTENT_MANAGER)
        self.client.force_login(cm)
        url = reverse("admin:notifications_outboundemail_changelist")
        self.assertEqual(self.client.get(url).status_code, 403)
        cm.user_permissions.add(Permission.objects.get(codename="view_operations"))
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:notifications_outboundemail_add")).status_code, 403)
