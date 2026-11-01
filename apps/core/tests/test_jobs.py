import json
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.audit.context import audit_scope
from apps.audit.models import AuditEvent
from apps.core.jobs import registry, schedules
from apps.core.jobs import services as jobs
from apps.core.models import Job, ScheduledTask, WorkerHeartbeat

from . import job_types

User = get_user_model()
S = Job.Status


def run_one(worker="w1"):
    job = jobs.claim_next(worker)
    return job, (jobs.run_job(job, worker) if job else None)


class RegistryTests(TestCase):
    def test_only_registered_types(self):
        with self.assertRaises(registry.UnknownJobTypeError):
            jobs.enqueue("os.system", {})

    def test_payload_validation(self):
        for payload in ({}, {"value": "1"}, {"value": True}, {"value": 1, "extra": 2}, ["value"]):
            with self.subTest(payload=payload), self.assertRaises(registry.InvalidPayloadError):
                jobs.enqueue("tests.noop", payload)
        uuid_type = registry.JobType("x", lambda p, j: None, {"id": "uuid"})
        with self.assertRaises(registry.InvalidPayloadError):
            registry.validate_payload(uuid_type, {"id": "not-a-uuid"})

    def test_registration_rules(self):
        with self.assertRaises(ValueError):
            registry.register("tests.bad", payload_schema={"f": "callable"})(lambda p, j: None)
        with self.assertRaises(ValueError):
            registry.register("tests.noop")(lambda p, j: None)  # Name already taken.


class EnqueueTests(TestCase):
    def test_idempotent_enqueue(self):
        first = jobs.enqueue("tests.noop", {"value": 1}, idempotency_key="k")
        second = jobs.enqueue("tests.noop", {"value": 2}, idempotency_key="k")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Job.objects.count(), 1)

    def test_rolled_back_transaction_leaves_no_job(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            jobs.enqueue("tests.noop", {"value": 1})
            raise RuntimeError
        self.assertFalse(Job.objects.exists())

    def test_correlation_id_from_scope(self):
        with audit_scope(request_id="corr-12345678"):
            job = jobs.enqueue("tests.noop", {"value": 1})
        self.assertEqual(job.request_id, "corr-12345678")


class ExecutionTests(TestCase):
    def test_success(self):
        job = jobs.enqueue("tests.noop", {"value": 1})
        claimed, status = run_one()
        self.assertEqual((claimed.pk, status), (job.pk, S.SUCCEEDED))
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts, job.worker_id), (S.SUCCEEDED, 1, "w1"))
        self.assertIsNotNone(job.finished_at)
        self.assertIsNone(job.lease_expires_at)
        self.assertEqual(job_types.EXECUTIONS[str(job.pk)], 1)
        self.assertFalse(AuditEvent.objects.exists())  # Routine successes are not audited.

    def test_not_claimed_before_run_after(self):
        jobs.enqueue("tests.noop", {"value": 1}, run_after=timezone.now() + timedelta(minutes=5))
        self.assertIsNone(jobs.claim_next("w1"))

    def test_priority_order(self):
        low = jobs.enqueue("tests.noop", {"value": 1}, priority=500)
        high = jobs.enqueue("tests.noop", {"value": 2}, priority=1)
        self.assertEqual(jobs.claim_next("w1").pk, high.pk)
        self.assertEqual(jobs.claim_next("w1").pk, low.pk)

    def test_retry_backoff_then_terminal_failure(self):
        job = jobs.enqueue("tests.flaky", {})
        for attempt in (1, 2):
            before = timezone.now()
            run_one()
            job.refresh_from_db()
            self.assertEqual((job.status, job.attempts, job.last_error_class), (S.RETRY_WAIT, attempt, "TRANSIENT"))
            delay = (job.run_after - before).total_seconds()
            cap = min(40, 10 * 2 ** (attempt - 1))
            self.assertTrue(cap / 2 - 1 <= delay <= cap + 1, delay)
            Job.objects.filter(pk=job.pk).update(run_after=timezone.now())
        run_one()
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts), (S.FAILED, 3))
        self.assertIn(job.pk, job_types.FAILURE_HOOK_CALLS)
        event = AuditEvent.objects.get(action="jobs.job.failed")
        self.assertEqual(event.metadata, {"job_type": "tests.flaky", "attempts": 3, "error_class": "TRANSIENT"})

    def test_permanent_error_fails_immediately(self):
        job = jobs.enqueue("tests.permanent", {})
        run_one()
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts, job.last_error), (S.FAILED, 1, "recipient rejected"))

    def test_unexpected_error_message_never_stored(self):
        job = jobs.enqueue("tests.leaky", {})
        run_one()
        job.refresh_from_db()
        self.assertEqual((job.last_error_class, job.last_error), ("UNEXPECTED", "ValueError"))
        stored = json.dumps(list(Job.objects.values()) + list(AuditEvent.objects.values()), default=str)
        self.assertNotIn("SECRET-123", stored)
        self.assertNotIn("victim@example.com", stored)

    def test_interrupted_worker_releases_job(self):
        job = jobs.enqueue("tests.interrupt", {})
        claimed = jobs.claim_next("w1")
        with self.assertRaises(KeyboardInterrupt):
            jobs.run_job(claimed, "w1")
        job.refresh_from_db()
        self.assertEqual((job.status, job.last_error_class, job.attempts), (S.RETRY_WAIT, "INTERRUPTED", 1))

    def test_backoff_bounds(self):
        self.assertEqual(jobs.compute_backoff(1, 30, 3600, rng=lambda: 0), 15)
        self.assertEqual(jobs.compute_backoff(1, 30, 3600, rng=lambda: 1), 30)
        self.assertEqual(jobs.compute_backoff(20, 30, 3600, rng=lambda: 1), 3600)


class LeaseTests(TestCase):
    def test_crashed_worker_job_recovered(self):
        job = jobs.enqueue("tests.noop", {"value": 1})
        jobs.claim_next("crashed")  # Worker dies without finishing.
        Job.objects.filter(pk=job.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(jobs.recover_expired_leases(), 1)
        job.refresh_from_db()
        self.assertEqual((job.status, job.last_error_class), (S.RETRY_WAIT, "LEASE_EXPIRED"))
        Job.objects.filter(pk=job.pk).update(run_after=timezone.now())
        run_one("survivor")
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts, job.worker_id), (S.SUCCEEDED, 2, "survivor"))

    def test_lease_recovery_fails_job_at_max_attempts(self):
        job = jobs.enqueue("tests.noop", {"value": 1}, max_attempts=1)
        jobs.claim_next("crashed")
        Job.objects.filter(pk=job.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        jobs.recover_expired_leases()
        job.refresh_from_db()
        self.assertEqual(job.status, S.FAILED)

    def test_worker_that_lost_its_lease_does_not_run_the_job(self):
        job = jobs.enqueue("tests.noop", {"value": 1})
        stale = jobs.claim_next("slow")
        Job.objects.filter(pk=job.pk).update(worker_id="other")  # Reclaimed elsewhere.
        jobs.run_job(stale, "slow")
        self.assertEqual(job_types.EXECUTIONS[str(job.pk)], 0)


class BatchLimitTests(TestCase):
    def test_max_jobs(self):
        for i in range(5):
            jobs.enqueue("tests.noop", {"value": i})
        self.assertEqual(jobs.process_jobs("w", max_jobs=3, max_seconds=60)["processed"], 3)

    def test_max_seconds(self):
        for i in range(5):
            jobs.enqueue("tests.noop", {"value": i})
        ticks = iter([0, 0, 10, 100, 100, 100])
        metrics = jobs.process_jobs("w", max_jobs=10, max_seconds=50, clock=lambda: next(ticks))
        self.assertEqual(metrics["processed"], 2)


class AdminActionServiceTests(TestCase):
    def setUp(self):
        self.root = User.objects.create_superuser("root@example.com", "Correct-Horse-Battery-9")

    def test_retry_and_cancel_rules(self):
        failed = jobs.enqueue("tests.permanent", {})
        run_one()
        with self.assertRaises(jobs.JobActionError):
            jobs.retry_job(jobs.enqueue("tests.noop", {"value": 1}), actor=self.root)  # Pending.
        retried = jobs.retry_job(failed, actor=self.root)
        self.assertEqual((retried.status, retried.attempts), (S.PENDING, 0))
        cancelled = jobs.cancel_job(retried, actor=self.root)
        self.assertEqual(cancelled.status, S.CANCELLED)
        with self.assertRaises(jobs.JobActionError):
            jobs.cancel_job(cancelled, actor=self.root)
        actions = list(AuditEvent.objects.filter(action__startswith="jobs.job.").values_list("action", flat=True))
        self.assertCountEqual(actions, ["jobs.job.failed", "jobs.job.retried", "jobs.job.cancelled"])

    def test_running_and_non_retryable_jobs(self):
        running = jobs.enqueue("tests.noop", {"value": 1})
        jobs.claim_next("w")
        with self.assertRaises(jobs.JobActionError):
            jobs.cancel_job(running, actor=self.root)
        locked = jobs.enqueue("tests.locked", {})
        Job.objects.filter(pk=running.pk).update(status=S.SUCCEEDED, lease_expires_at=None)
        run_one()
        with self.assertRaises(jobs.JobActionError):
            jobs.retry_job(locked, actor=self.root)


class ScheduleTests(TestCase):
    def test_due_tasks_enqueued_once_per_slot(self):
        now = timezone.now()
        self.assertEqual(schedules.enqueue_due_schedules(now), len(schedules.SCHEDULES))
        self.assertEqual(schedules.enqueue_due_schedules(now), 0)  # Not due again yet.
        self.assertEqual(Job.objects.filter(idempotency_key__startswith="schedule:").count(), len(schedules.SCHEDULES))
        task = ScheduledTask.objects.get(name="core.health_check")
        self.assertAlmostEqual((task.next_run_at - now).total_seconds(), 300, delta=1)

    def test_disabled_task_skipped(self):
        schedules.enqueue_due_schedules()
        ScheduledTask.objects.update(enabled=False, next_run_at=timezone.now())
        self.assertEqual(schedules.enqueue_due_schedules(), 0)

    @override_settings(JOBS_SCHEDULE_INTERVALS={"core.health_check": 900})
    def test_interval_override(self):
        now = timezone.now()
        schedules.enqueue_due_schedules(now)
        task = ScheduledTask.objects.get(name="core.health_check")
        self.assertAlmostEqual((task.next_run_at - now).total_seconds(), 900, delta=1)


class CommandTests(TestCase):
    def test_run_jobs_processes_schedules_and_records_heartbeat(self):
        jobs.enqueue("tests.noop", {"value": 1})
        out = StringIO()
        call_command("run_jobs", "--worker-name", "cron-1", "--max-seconds", "30", stdout=out)
        summary = json.loads(out.getvalue())
        self.assertEqual(summary["worker"], "cron-1")
        self.assertGreaterEqual(summary["succeeded"], 1 + len(schedules.SCHEDULES))
        heartbeat = WorkerHeartbeat.objects.get(pk="cron-1")
        self.assertTrue(heartbeat.cache_ok)  # Set by the scheduled health check.
        self.assertGreater(heartbeat.total_processed, 0)

        status = StringIO()
        call_command("jobs_status", stdout=status)
        self.assertTrue(json.loads(status.getvalue())["healthy"])

    def test_jobs_status_unhealthy_without_heartbeat(self):
        with self.assertRaises(CommandError):
            call_command("jobs_status", stdout=StringIO())

    def test_jobs_status_reports_cache_outage(self):
        call_command("run_jobs", "--worker-name", "cron-1", stdout=StringIO())
        with mock.patch("apps.core.management.commands.jobs_status.probe_cache", return_value=False):
            with self.assertRaises(CommandError):
                call_command("jobs_status", stdout=StringIO())

    def test_prune_jobs(self):
        old = jobs.enqueue("tests.noop", {"value": 1})
        run_one()
        Job.objects.filter(pk=old.pk).update(finished_at=timezone.now() - timedelta(days=31))
        pending = jobs.enqueue("tests.noop", {"value": 2}, run_after=timezone.now() + timedelta(days=1))
        jobs.enqueue("core.prune_finished_jobs", {})
        run_one()
        self.assertFalse(Job.objects.filter(pk=old.pk).exists())
        self.assertTrue(Job.objects.filter(pk=pending.pk).exists())
