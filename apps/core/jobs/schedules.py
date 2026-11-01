"""
Code-defined recurring tasks. ``run_jobs`` (run by cron) enqueues due tasks
before processing the queue. Intervals can be overridden with
settings.JOBS_SCHEDULE_INTERVALS ({name: seconds}); tasks can be paused in the
admin (audited). Each due slot is enqueued once (idempotency key per slot).
"""

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import ScheduledTask
from .services import enqueue


@dataclass(frozen=True)
class Schedule:
    name: str
    job_type: str
    interval_seconds: int
    description: str


SCHEDULES = {
    s.name: s
    for s in (
        Schedule("core.health_check", "core.health_check", 300, "Probe the shared cache; record worker health."),
        Schedule("core.detect_stale_jobs", "core.detect_stale_jobs", 300, "Log overdue jobs and expired leases."),
        Schedule("core.prune_rate_limits", "core.prune_rate_limits", 3600, "Delete expired rate-limit counters."),
        Schedule("core.prune_finished_jobs", "core.prune_finished_jobs", 86400, "Delete old finished jobs."),
        Schedule("notifications.prune_outbox", "notifications.prune_outbox", 86400, "Delete old delivered emails."),
    )
}


def interval_for(schedule):
    seconds = int(settings.JOBS_SCHEDULE_INTERVALS.get(schedule.name, schedule.interval_seconds))
    return max(60, seconds)


def enqueue_due_schedules(now=None):
    now = now or timezone.now()
    for name in SCHEDULES:
        ScheduledTask.objects.get_or_create(name=name, defaults={"next_run_at": now})
    enqueued = 0
    with transaction.atomic():
        due = ScheduledTask.objects.select_for_update(skip_locked=True).filter(
            enabled=True, next_run_at__lte=now, name__in=list(SCHEDULES)
        )
        for task in due:
            schedule = SCHEDULES[task.name]
            enqueue(schedule.job_type, {}, idempotency_key=f"schedule:{task.name}:{int(task.next_run_at.timestamp())}")
            task.next_run_at = now + timedelta(seconds=interval_for(schedule))
            task.last_enqueued_at = now
            task.save(update_fields=["next_run_at", "last_enqueued_at"])
            enqueued += 1
    return enqueued
