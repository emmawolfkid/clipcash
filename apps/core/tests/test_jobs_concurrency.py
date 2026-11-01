import threading
from unittest import skipUnless

from django.db import connection, connections
from django.test import TransactionTestCase

from apps.core.jobs import services as jobs
from apps.core.models import Job

from . import job_types


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


@skipUnless(connection.vendor == "postgresql", "SKIP LOCKED claiming is verified on PostgreSQL.")
class ConcurrentWorkerTests(TransactionTestCase):
    def test_concurrent_workers_run_each_job_exactly_once(self):
        created = [jobs.enqueue("tests.noop", {"value": i}) for i in range(40)]
        results = run_threads(4, lambda i: jobs.process_jobs(f"worker-{i}", max_jobs=40, max_seconds=60))
        self.assertTrue(all(isinstance(r, dict) for r in results), results)
        self.assertEqual(sum(r["succeeded"] for r in results), 40)
        for job in created:
            self.assertEqual(job_types.EXECUTIONS[str(job.pk)], 1, job.pk)
        self.assertEqual(Job.objects.filter(status=Job.Status.SUCCEEDED).count(), 40)
        self.assertGreater(len({j.worker_id for j in Job.objects.all()}), 1)

    def test_single_job_claimed_by_only_one_worker(self):
        job = jobs.enqueue("tests.noop", {"value": 1})
        claims = run_threads(6, lambda i: jobs.claim_next(f"worker-{i}"))
        claimed = [c for c in claims if isinstance(c, Job)]
        self.assertEqual(len(claimed), 1, claims)
        self.assertEqual(claimed[0].pk, job.pk)
