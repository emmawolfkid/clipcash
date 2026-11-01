import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase

User = get_user_model()

PASSWORD = "Correct-Horse-Battery-9"
NEW_PASSWORD = "Another-Strong-Phrase-42"


def make_user(
    email="member@example.com",
    *,
    role=User.Role.MEMBER,
    status=User.Status.ACTIVE,
    verified=True,
    password=PASSWORD,
):
    if role == User.Role.SUPER_ADMIN:
        user = User.objects.create_superuser(email, password)
    else:
        user = User.objects.create_user(email, password, role=role, status=status)
    if user.status != status or user.email_verified != verified:
        user.status = status
        user.email_verified = verified
        user.save()
    return user


def run_worker():
    """Process due background jobs (e.g. outbox emails) as cron's run_jobs would."""
    from apps.audit.context import audit_scope
    from apps.core.jobs.services import process_jobs

    with audit_scope(source="SYSTEM"):
        return process_jobs("test-worker", max_jobs=100, max_seconds=30)


class DeliveringClient(Client):
    """Test client that runs the worker after each request, like a prompt cron
    worker, so emails queued in the outbox are delivered to mail.outbox."""

    def request(self, **request):
        response = super().request(**request)
        run_worker()
        return response


def links_in_last_email():
    return re.findall(r"https?://\S+", mail.outbox[-1].body)


class AccountsTestCase(TestCase):
    """Clears the rate-limit cache so tests are independent; delivers queued
    email after each request."""

    client_class = DeliveringClient

    def setUp(self):
        super().setUp()
        cache.clear()
