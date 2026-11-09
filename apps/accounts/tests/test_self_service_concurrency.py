"""Phase 17-A: the email-change and profile services under real concurrent transactions."""

import threading
import time
from unittest import skipUnless

from django.core.cache import cache
from django.db import IntegrityError, connection, connections, transaction
from django.test import RequestFactory, TransactionTestCase

from apps.accounts import services
from apps.accounts.models import PendingEmailChange
from apps.accounts.tokens import email_change_token_generator
from apps.audit.models import AuditEvent
from apps.audit.tests.test_concurrency import run_concurrently

from .helpers import NEW_PASSWORD, make_user

NEW_EMAIL = "new-address@example.com"


def wait_until_a_session_waits_for_a_lock(timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            # pg_locks is live; pg_stat_activity would stay cached inside the caller's transaction.
            cursor.execute("SELECT count(*) FROM pg_locks WHERE NOT granted")
            if cursor.fetchone()[0]:
                return
        time.sleep(0.02)
    raise AssertionError("No session started waiting for a lock.")


@skipUnless(connection.vendor == "postgresql", "Concurrency is verified on PostgreSQL.")
class EmailChangeConcurrencyTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.member = make_user()
        self.request = RequestFactory().post("/", HTTP_HOST="localhost")

    def pending(self, user=None, new_email=NEW_EMAIL):
        change = services.request_email_change(user or self.member, new_email, self.request)
        return change, email_change_token_generator.make_token(change)

    def test_a_link_confirmed_concurrently_succeeds_exactly_once(self):
        change, token = self.pending()
        results = []
        errors = run_concurrently(5, lambda _: results.append(services.confirm_email_change(change.pk, token, self.member)))
        self.assertEqual(errors, [])
        self.assertEqual(sum(result is not None for result in results), 1)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, NEW_EMAIL)
        self.assertTrue(self.member.email_verified)
        self.assertEqual(AuditEvent.objects.filter(action="account.email_changed").count(), 1)
        self.assertFalse(PendingEmailChange.objects.exists())

    def test_two_accounts_confirming_one_address_cannot_both_get_it(self):
        other = make_user("other@example.com")
        links = [self.pending(), self.pending(other)]
        owners = [self.member, other]
        results = {}

        def confirm(index):
            change, token = links[index]
            results[index] = services.confirm_email_change(change.pk, token, owners[index])

        errors = run_concurrently(2, confirm)
        self.assertEqual(errors, [])
        self.assertEqual(sum(result is not None for result in results.values()), 1)
        self.member.refresh_from_db()
        other.refresh_from_db()
        emails = {self.member.email, other.email}
        self.assertEqual(len(emails), 2)
        self.assertIn(NEW_EMAIL, emails)
        self.assertEqual(len(emails & {"member@example.com", "other@example.com"}), 1)  # The loser keeps theirs.

    def test_only_the_owner_can_complete_a_link_even_concurrently(self):
        """RG-03: the service takes the confirming account; another account holding the same
        token is refused, whatever the timing, and does not consume the link."""
        change, token = self.pending()
        intruder = make_user("intruder@example.com")
        results = {}

        def confirm(index):
            results[index] = services.confirm_email_change(change.pk, token, (intruder, self.member)[index % 2])

        errors = run_concurrently(6, confirm)
        self.assertEqual(errors, [])
        winners = [index for index, result in results.items() if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0] % 2, 1)  # The owner.
        intruder.refresh_from_db()
        self.member.refresh_from_db()
        self.assertEqual((self.member.email, intruder.email), (NEW_EMAIL, "intruder@example.com"))

    def test_an_intruder_alone_leaves_the_link_usable(self):
        change, token = self.pending()
        self.assertIsNone(services.confirm_email_change(change.pk, token, make_user("intruder@example.com")))
        self.assertIsNone(services.confirm_email_change(change.pk, token, None))
        self.assertTrue(PendingEmailChange.objects.filter(pk=change.pk).exists())
        self.assertIsNotNone(services.confirm_email_change(change.pk, token, self.member))

    def test_concurrent_requests_leave_one_waiting_change(self):
        errors = run_concurrently(
            6, lambda index: services.request_email_change(self.member, f"new{index}@example.com", self.request))
        self.assertEqual(errors, [])
        self.assertEqual(PendingEmailChange.objects.filter(user=self.member).count(), 1)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, "member@example.com")

    def test_password_change_and_confirmation_cannot_deadlock(self):
        """Both orders meet here: the password change holds the account and then ends the
        request, so confirmation must take the account first as well. Otherwise PostgreSQL
        aborts one side with a deadlock and the person sees a server error."""
        change, token = self.pending()
        account_locked = threading.Event()
        results, errors = [], []

        def change_password():
            try:
                with transaction.atomic():
                    account = type(self.member).objects.get(pk=self.member.pk)
                    account.set_password(NEW_PASSWORD)
                    account.save()
                    account_locked.set()
                    wait_until_a_session_waits_for_a_lock()
                    services.discard_email_change(account)
            except Exception as error:  # noqa: BLE001 - collected for assertions
                errors.append(error)
            finally:
                connections.close_all()

        def confirm():
            try:
                account_locked.wait(10)
                results.append(services.confirm_email_change(change.pk, token, self.member))
            except Exception as error:  # noqa: BLE001 - collected for assertions
                errors.append(error)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=change_password), threading.Thread(target=confirm)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(results, [None])  # The password change came first, so the link is dead.
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, "member@example.com")
        self.assertTrue(self.member.check_password(NEW_PASSWORD))
        self.assertFalse(PendingEmailChange.objects.exists())


@skipUnless(connection.vendor == "postgresql", "Concurrency is verified on PostgreSQL.")
class ProfileConcurrencyTests(TransactionTestCase):
    def test_one_phone_number_cannot_reach_two_accounts(self):
        cache.clear()
        users = [make_user("first@example.com"), make_user("second@example.com")]
        phone = "+255712345678"
        errors = run_concurrently(2, lambda index: services.update_profile(
            users[index], first_name="", last_name="", phone_number=phone))
        # The view turns this IntegrityError into the ordinary "cannot be used" form error.
        self.assertEqual([type(error) for error in errors], [IntegrityError])
        for user in users:
            user.refresh_from_db()
        self.assertEqual([user.phone_number for user in users].count(phone), 1)
