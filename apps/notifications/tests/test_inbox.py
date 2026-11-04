"""Phase 15-A: in-app notification creation, delivery and the inbox pages."""

import threading
from unittest import skipUnless

from django.db import IntegrityError, connection, connections, transaction
from django.test import Client, TransactionTestCase
from django.urls import reverse

from apps.accounts.tests.helpers import AccountsTestCase, User, make_user
from apps.notifications import inbox
from apps.notifications.models import Notification, OutboundEmail

C = Notification.Category


def notify(user, key="event:1", **values):
    values = {"category": C.ACCOUNT, "title": "Hello", "message": "Body", **values}
    return inbox.notify(user, "test.event", dedupe_key=key, **values)


class CreationTests(AccountsTestCase):
    def setUp(self):
        super().setUp()
        self.member = make_user("member@example.com")

    def test_notice_goes_to_the_named_recipient_only(self):
        other = make_user("other@example.com")
        self.assertEqual(notify(self.member), 1)
        self.assertEqual(Notification.objects.get().recipient, self.member)
        self.assertFalse(Notification.objects.filter(recipient=other).exists())

    def test_inactive_accounts_are_skipped(self):
        suspended = make_user("s@example.com", status=User.Status.SUSPENDED)
        self.assertEqual(notify([suspended, None, self.member]), 1)

    def test_same_event_twice_creates_one_notice_and_one_email(self):
        self.assertEqual(notify(self.member, email=True), 1)
        self.assertEqual(notify(self.member, email=True), 0)  # Retry, repeated job, replayed request.
        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(OutboundEmail.objects.filter(kind=inbox.NOTICE_EMAIL).count(), 1)

    def test_rolled_back_business_change_leaves_no_notice_or_email(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            notify(self.member, email=True)
            raise RuntimeError("business step failed")
        self.assertFalse(Notification.objects.exists())
        self.assertFalse(OutboundEmail.objects.exists())

    def test_duplicate_inside_a_caller_transaction_does_not_break_it(self):
        with transaction.atomic():
            notify(self.member)
            Notification.objects.filter(recipient=self.member).update(dedupe_key="other")  # Hide from pre-check.
            Notification.objects.filter(recipient=self.member).update(dedupe_key="event:1")
            notify(self.member)  # Hits the unique constraint inside its own savepoint.
            make_user("after@example.com")  # The caller's transaction is still usable.
        self.assertEqual(Notification.objects.count(), 1)

    def test_only_internal_links_are_kept(self):
        for link, stored in (("/memberships/mine/", "/memberships/mine/"), ("https://evil.example/x", ""),
                             ("//evil.example/x", ""), ("/\\evil.example", ""), ("javascript:alert(1)", "")):
            with self.subTest(link=link):
                notify(self.member, key=f"link:{link}", link=link)
                self.assertEqual(Notification.objects.get(dedupe_key=f"link:{link}").link, stored)

    def test_database_refuses_external_links_and_unknown_categories(self):
        for values in ({"link": "//evil.example"}, {"link": "https://evil.example"}, {"category": "MONEY"}):
            with self.subTest(values=values), self.assertRaises(IntegrityError), transaction.atomic():
                Notification.objects.create(recipient=self.member, kind="x", title="x", dedupe_key=str(values),
                                            **{"category": C.ACCOUNT, **values})

    def test_notice_email_carries_no_link_and_skips_removed_notices(self):
        from django.core import mail

        from apps.accounts.tests.helpers import run_worker

        notify(self.member, link="/memberships/mine/", email=True, title="Plan update", message="Details here.")
        notify(self.member, key="event:2", email=True)
        Notification.objects.filter(dedupe_key="event:2").delete()
        run_worker()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "ClipCash: Plan update")
        self.assertNotIn("http", mail.outbox[0].body)
        self.assertEqual(OutboundEmail.objects.get(context__notification__isnull=False, status="SKIPPED").skip_reason,
                         "notification_removed")


@skipUnless(connection.vendor == "postgresql", "Concurrency is verified on PostgreSQL.")
class ConcurrencyTests(TransactionTestCase):
    def test_parallel_producers_create_one_notice_and_one_email(self):
        member = make_user("member@example.com")
        barrier = threading.Barrier(4)
        results = []

        def produce():
            try:
                barrier.wait()
                with transaction.atomic():
                    results.append(notify(member, key="race:1", email=True))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=produce) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [0, 0, 0, 1])
        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(OutboundEmail.objects.filter(kind=inbox.NOTICE_EMAIL).count(), 1)


class InboxPageTests(AccountsTestCase):
    def setUp(self):
        super().setUp()
        self.member = make_user("member@example.com")
        self.other = make_user("other@example.com")
        self.client.force_login(self.member)

    def test_shows_only_own_notices_with_filters_and_empty_states(self):
        self.assertContains(self.client.get(reverse("notifications:inbox")), "You have no notifications yet")
        notify(self.member, key="a", title="Mine account", category=C.ACCOUNT)
        notify(self.member, key="b", title="Mine membership", category=C.MEMBERSHIP)
        notify(self.other, key="c", title="Someone else's")
        page = self.client.get(reverse("notifications:inbox"))
        self.assertContains(page, "Mine account")
        self.assertNotContains(page, "Someone else&#x27;s")
        filtered = self.client.get(reverse("notifications:inbox"), {"category": "MEMBERSHIP"})
        self.assertContains(filtered, "Mine membership")
        self.assertNotContains(filtered, "Mine account")
        inbox.mark_all_read(self.member)
        self.assertContains(self.client.get(reverse("notifications:inbox"), {"unread": "1"}),
                            "No notifications match this filter.")

    def test_header_bell_shows_the_unread_count(self):
        for index in range(3):
            notify(self.member, key=f"n{index}")
        page = self.client.get(reverse("notifications:inbox"))
        self.assertContains(page, 'aria-label="Notifications, 3 unread"')
        self.assertContains(page, '<span class="notification-bell__badge" aria-hidden="true">3</span>', html=False)

    def test_mark_one_and_all_read(self):
        notify(self.member, key="a")
        notify(self.member, key="b")
        first = Notification.objects.get(dedupe_key="a")
        response = self.client.post(reverse("notifications:read", args=[first.pk]),
                                    {"next": "/notifications/?unread=1"})
        self.assertRedirects(response, "/notifications/?unread=1", fetch_redirect_response=False)
        self.assertEqual(inbox.unread_count(self.member), 1)
        self.client.post(reverse("notifications:read_all"))
        self.assertEqual(inbox.unread_count(self.member), 0)

    def test_open_marks_read_and_goes_only_to_internal_links(self):
        notify(self.member, key="a", link=reverse("memberships:mine"))
        notify(self.member, key="b")
        with_link, without = Notification.objects.get(dedupe_key="a"), Notification.objects.get(dedupe_key="b")
        self.assertRedirects(self.client.post(reverse("notifications:open", args=[with_link.pk])),
                             reverse("memberships:mine"), fetch_redirect_response=False)
        self.assertRedirects(self.client.post(reverse("notifications:open", args=[without.pk])),
                             reverse("notifications:inbox"))
        self.assertTrue(Notification.objects.get(pk=with_link.pk).is_read)
        self.assertTrue(Notification.objects.get(pk=without.pk).is_read)

    def test_unsafe_next_parameter_is_ignored(self):
        notify(self.member, key="a")
        notice = Notification.objects.get()
        for target in ("https://evil.example/", "//evil.example/", "/accounts/logout/"):
            with self.subTest(target=target):
                response = self.client.post(reverse("notifications:read", args=[notice.pk]), {"next": target})
                self.assertRedirects(response, reverse("notifications:inbox"))

    def test_other_accounts_notices_are_not_found_and_unchanged(self):
        notify(self.other, key="theirs")
        theirs = Notification.objects.get()
        for name in ("notifications:open", "notifications:read"):
            with self.subTest(name=name):
                self.assertEqual(self.client.post(reverse(name, args=[theirs.pk])).status_code, 404)
        self.client.post(reverse("notifications:read_all"))
        self.assertFalse(Notification.objects.get(pk=theirs.pk).is_read)

    def test_state_changes_need_post_and_csrf(self):
        notify(self.member, key="a")
        notice = Notification.objects.get()
        self.assertEqual(self.client.get(reverse("notifications:read", args=[notice.pk])).status_code, 405)
        self.assertEqual(self.client.get(reverse("notifications:read_all")).status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.member)
        self.assertEqual(strict.post(reverse("notifications:read_all")).status_code, 403)
        self.assertFalse(Notification.objects.get().is_read)

    def test_sign_in_required(self):
        self.client.logout()
        response = self.client.get(reverse("notifications:inbox"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_pagination(self):
        for index in range(25):
            notify(self.member, key=f"n{index}", title=f"Notice {index}")
        first = self.client.get(reverse("notifications:inbox"))
        self.assertEqual(len(first.context["page"].object_list), 20)
        self.assertContains(first, "Page 1 of 2")
        self.assertEqual(len(self.client.get(reverse("notifications:inbox"), {"page": 2}).context["page"].object_list), 5)

    def test_staff_use_the_same_inbox(self):
        staff = make_user("cm@example.com", role=User.Role.CONTENT_MANAGER)
        notify(staff, key="ops", title="Staff notice", category=C.OPERATIONS)
        self.client.force_login(staff)
        self.assertContains(self.client.get(reverse("notifications:inbox")), "Staff notice")
