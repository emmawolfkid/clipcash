import threading
from unittest import skipUnless

from django.db import connection, connections, transaction
from django.test import TransactionTestCase

from apps.audit import services
from apps.audit.models import AuditEvent


def run_concurrently(count, func):
    barrier = threading.Barrier(count)
    errors = []

    def worker(index):
        try:
            barrier.wait()
            func(index)
        except Exception as error:  # noqa: BLE001 - collected for assertions
            errors.append(error)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


@skipUnless(connection.vendor == "postgresql", "Concurrency is verified on PostgreSQL.")
class ConcurrentAuditWriteTests(TransactionTestCase):
    def test_concurrent_security_events_all_written(self):
        errors = run_concurrently(
            10, lambda i: services.record_security_event("auth.logout", outcome=services.SUCCESS, actor=None)
        )
        self.assertEqual(errors, [])
        self.assertEqual(AuditEvent.objects.filter(action="auth.logout").count(), 10)

    def test_concurrent_idempotent_writes_store_one_event(self):
        def write(index):
            with transaction.atomic():
                services.record_business_event(
                    "audit.purged", actor=None, metadata={"count": index}, idempotency_key="same-key"
                )

        errors = run_concurrently(6, write)
        self.assertEqual(errors, [])
        self.assertEqual(AuditEvent.objects.filter(idempotency_key="same-key").count(), 1)
