"""
Run one bounded batch of background work. Designed for cron, e.g. every minute:

    * * * * *  cd /path/to/clipcash && venv/bin/python manage.py run_jobs

Each run: records a heartbeat, recovers expired leases, enqueues due scheduled
tasks, then processes jobs until --max-jobs or --max-seconds is reached.
Overlapping runs are safe: jobs are claimed with SKIP LOCKED.
"""

import json
import time
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.audit.context import audit_scope
from apps.core.jobs import schedules
from apps.core.jobs.services import default_worker_id, process_jobs, record_heartbeat, recover_expired_leases


class Command(BaseCommand):
    help = "Process one bounded batch of background jobs (run from cron)."

    def add_arguments(self, parser):
        parser.add_argument("--max-seconds", type=float, default=None,
                            help="Stop claiming jobs after this many seconds.")
        parser.add_argument("--max-jobs", type=int, default=None, help="Process at most this many jobs.")
        parser.add_argument("--worker-name", default=None, help="Heartbeat name (default: host name).")
        parser.add_argument("--no-schedule", action="store_true", help="Do not enqueue scheduled tasks.")

    def handle(self, *args, max_seconds, max_jobs, worker_name, no_schedule, **options):
        max_seconds = settings.JOBS_MAX_RUN_SECONDS if max_seconds is None else max_seconds
        max_jobs = settings.JOBS_MAX_BATCH if max_jobs is None else max_jobs
        worker_id = (worker_name or default_worker_id())[:100]
        started = timezone.now()
        clock_start = time.monotonic()

        with audit_scope(source="SYSTEM", request_id=f"run-{uuid.uuid4().hex}"):
            record_heartbeat(worker_id, started_at=started)
            recovered = recover_expired_leases()
            scheduled = 0 if no_schedule else schedules.enqueue_due_schedules()
            metrics = process_jobs(worker_id, max_jobs=max_jobs, max_seconds=max_seconds)
            elapsed = time.monotonic() - clock_start
            record_heartbeat(worker_id, run_seconds=elapsed, metrics=metrics)

        summary = {"worker": worker_id, "recovered_leases": recovered, "scheduled": scheduled,
                   **metrics, "seconds": round(elapsed, 2)}
        self.stdout.write(json.dumps(summary))
