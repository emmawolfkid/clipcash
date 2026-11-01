import threading
from datetime import timedelta
from unittest import mock, skipUnless

from django.db import connection, connections
from django.test import TransactionTestCase
from django.utils import timezone

from apps.accounts.tests.helpers import make_user
from apps.audit.models import AuditEvent
from apps.memberships import services
from apps.memberships.models import Subscription

from .helpers import payment_for, payments_enabled


def run_threads(count, target):
    barrier = threading.Barrier(count)
    results = [None] * count

    def worker(index):
        try:
            barrier.wait()
            results[index] = target(index)
        except Exception as error:  # noqa: BLE001
            results[index] = error
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


@skipUnless(connection.vendor == "postgresql", "Concurrency is verified on PostgreSQL.")
class MembershipConcurrencyTests(TransactionTestCase):
    def setUp(self):
        from apps.accounts.tests.helpers import User

        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.member = make_user("member@example.com")
        gate = mock.patch("apps.memberships.services._feature_enabled", return_value=True)
        gate.start()
        self.addCleanup(gate.stop)
        self.plan = services.create_plan(self.root, code="monthly", name="Monthly", price_amount=5_000,
                                         duration_days=30, entitlements=["videos.standard"])

    def test_concurrent_choices_create_one_pending(self):
        results = run_threads(5, lambda i: services.choose_plan(self.member, self.plan))
        self.assertTrue(all(isinstance(r, Subscription) for r in results), results)
        self.assertEqual(len({r.pk for r in results}), 1)
        self.assertEqual(Subscription.objects.filter(member=self.member, status="PENDING").count(), 1)

    def test_concurrent_payments_activate_once(self):
        pending = services.choose_plan(self.member, self.plan)
        with payments_enabled():
            results = run_threads(4, lambda i: services.activate_with_verified_payment(
                pending.pk, payment_for(pending, reference=f"PAY-{i}")))
        activated = [r for r in results if isinstance(r, Subscription)]
        self.assertEqual(len(activated), 1, results)
        self.assertTrue(all(isinstance(r, services.MembershipError) for r in results if r not in activated))
        self.assertEqual(Subscription.objects.filter(member=self.member, status="ACTIVE").count(), 1)

    def test_concurrent_expiry_runs_expire_each_once(self):
        members = [make_user(f"m{i}@example.com") for i in range(6)]
        with payments_enabled():
            for member in members:
                pending = services.choose_plan(member, self.plan)
                services.activate_with_verified_payment(pending.pk, payment_for(pending))
        future = timezone.now() + timedelta(days=31)
        run_threads(3, lambda i: services.process_expiry(now=future))
        self.assertEqual(Subscription.objects.filter(status="EXPIRED").count(), 6)
        self.assertEqual(AuditEvent.objects.filter(action="membership.subscription.expired").count(), 6)

    def test_concurrent_renewal_cannot_overlap(self):
        with payments_enabled():
            current = services.activate_with_verified_payment(
                services.choose_plan(self.member, self.plan).pk,
                payment_for(Subscription.objects.get(member=self.member, status="PENDING")))
        later = timezone.now() + timedelta(days=20)
        with mock.patch("apps.memberships.services.timezone.now", return_value=later), payments_enabled():
            renewal = services.choose_plan(self.member, self.plan)
            results = run_threads(3, lambda i: services.activate_with_verified_payment(
                renewal.pk, payment_for(renewal, reference=f"REN-{i}")))
        self.assertEqual(sum(isinstance(r, Subscription) for r in results), 1, results)
        live = Subscription.objects.filter(member=self.member, status="ACTIVE").order_by("starts_at")
        self.assertEqual(live.count(), 2)
        self.assertEqual(live[1].starts_at, current.expires_at)
