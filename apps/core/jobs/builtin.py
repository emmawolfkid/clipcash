"""Built-in maintenance job types (registered when the core app is ready)."""

import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from ..models import Job, RateLimitCounter
from .registry import register
from .services import record_heartbeat

log = logging.getLogger("clipcash.jobs")


def probe_cache():
    """True if the shared cache can store, read and delete a value."""
    key = f"core:health:{uuid.uuid4().hex}"
    try:
        cache.set(key, "ok", timeout=30)
        healthy = cache.get(key) == "ok"
        cache.delete(key)
        return healthy
    except Exception:  # noqa: BLE001 - any backend failure means unhealthy
        log.exception("cache_probe_failed")
        return False


@register("core.health_check", max_attempts=1, admin_retryable=False,
          description="Probe the shared cache and record worker health.")
def health_check(payload, job):
    healthy = probe_cache()
    record_heartbeat(job.worker_id, cache_ok=healthy)
    if not healthy:
        log.error("cache_unavailable worker=%s", job.worker_id)


@register("core.detect_stale_jobs", max_attempts=1, admin_retryable=False,
          description="Log overdue jobs and expired leases.")
def detect_stale_jobs(payload, job):
    now = timezone.now()
    overdue = Job.objects.filter(
        status__in=Job.RUNNABLE, run_after__lt=now - timedelta(seconds=settings.JOBS_BACKLOG_ALERT_SECONDS)
    ).count()
    expired = Job.objects.filter(status=Job.Status.RUNNING, lease_expires_at__lt=now).count()
    if overdue or expired:
        log.error("job_queue_stale overdue=%s expired_leases=%s", overdue, expired)


@register("core.prune_rate_limits", max_attempts=2, admin_retryable=False,
          description="Delete expired rate-limit counters.")
def prune_rate_limits(payload, job):
    RateLimitCounter.objects.filter(expires_at__lt=timezone.now()).delete()


@register("core.prune_finished_jobs", max_attempts=2, admin_retryable=False,
          description="Delete finished jobs past their retention period.")
def prune_finished_jobs(payload, job):
    now = timezone.now()
    Job.objects.filter(
        status__in=[Job.Status.SUCCEEDED, Job.Status.CANCELLED],
        finished_at__lt=now - timedelta(days=settings.JOBS_RETENTION_DAYS),
    ).delete()
    # Failed jobs are kept longer for investigation; each also has an audit event.
    Job.objects.filter(
        status=Job.Status.FAILED,
        finished_at__lt=now - timedelta(days=settings.JOBS_FAILED_RETENTION_DAYS),
    ).delete()
