"""
PostgreSQL-backed job queue.

Lifecycle:  PENDING -> RUNNING -> SUCCEEDED
                          |-> RETRY_WAIT -> RUNNING ...   (backoff with jitter)
                          |-> FAILED                      (max attempts / permanent)
            PENDING / RETRY_WAIT / FAILED -> CANCELLED    (administrator)
            FAILED / CANCELLED -> PENDING                 (administrator retry)

Claiming uses SELECT ... FOR UPDATE SKIP LOCKED inside a short transaction, so
concurrent workers never claim the same job. A claimed job carries a lease;
the job row stays locked while its handler runs, so lease recovery (which also
skips locked rows) only reclaims jobs whose worker died.

Delivery is AT LEAST ONCE: a worker can crash after an external action (e.g.
sending an email) but before recording success, and the job will run again.
Handlers must be idempotent, and external effects need provider-side
idempotency or reconciliation.
"""

import logging
import random
import socket
import time
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Min
from django.utils import timezone

from apps.audit import context as audit_context
from apps.audit.services import record_business_event

from ..models import Job, WorkerHeartbeat
from .errors import JobError
from .registry import get_job_type, validate_payload

log = logging.getLogger("clipcash.jobs")

S = Job.Status


# --- Enqueueing -----------------------------------------------------------------


def enqueue(job_type, payload=None, *, run_after=None, idempotency_key=None, priority=None, max_attempts=None):
    """
    Add a job. Safe inside a business transaction: the job commits or rolls
    back with it. With an idempotency key, a repeat returns the existing job.
    """
    definition = get_job_type(job_type)
    cleaned = validate_payload(definition, payload)
    if idempotency_key is not None:
        idempotency_key = str(idempotency_key)[:200]
        existing = Job.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return existing
    context = audit_context.current()
    job = Job(
        job_type=job_type,
        payload=cleaned,
        priority=definition.priority if priority is None else priority,
        run_after=run_after or timezone.now(),
        max_attempts=max_attempts or definition.max_attempts,
        idempotency_key=idempotency_key,
        request_id=context.request_id if context else "",
    )
    try:
        with transaction.atomic():
            job.save(force_insert=True)
    except IntegrityError:
        if idempotency_key is None:
            raise
        return Job.objects.get(idempotency_key=idempotency_key)
    return job


# --- Claiming and running ----------------------------------------------------------


def default_worker_id():
    return f"{socket.gethostname()}"[:100]


def compute_backoff(attempt, base_seconds, max_seconds, rng=random.random):
    """Exponential backoff with 'equal jitter': half fixed, half random."""
    cap = min(max_seconds, base_seconds * (2 ** max(0, attempt - 1)))
    return cap / 2 + rng() * cap / 2


def claim_next(worker_id, now=None):
    """Atomically claim the next runnable job, or return None."""
    now = now or timezone.now()
    with transaction.atomic():
        job = (
            Job.objects.select_for_update(skip_locked=True)
            .filter(status__in=Job.RUNNABLE, run_after__lte=now)
            .order_by("priority", "run_after", "created_at")
            .first()
        )
        if job is None:
            return None
        job.status = S.RUNNING
        job.attempts += 1
        job.started_at = now
        job.finished_at = None
        job.lease_expires_at = now + timedelta(seconds=settings.JOBS_LEASE_SECONDS)
        job.worker_id = worker_id
        job.save(update_fields=["status", "attempts", "started_at", "finished_at",
                                "lease_expires_at", "worker_id", "updated_at"])
    return job


def sanitize_error(error):
    """(error_class, summary) safe to store: never another exception's message."""
    if isinstance(error, JobError):
        return error.error_class, error.safe_message or type(error).__name__
    return Job.ErrorClass.UNEXPECTED, type(error).__name__


def _fail_or_retry(job, error_class, summary, now, *, permanent=False):
    """Inside a transaction, with ``job`` locked: schedule a retry or fail terminally."""
    definition = get_job_type(job.job_type)
    job.last_error_class = error_class
    job.last_error = summary[:300]
    job.lease_expires_at = None
    if permanent or job.attempts >= job.max_attempts:
        job.status = S.FAILED
        job.finished_at = now
        job.save()
        if definition.on_failure is not None:
            definition.on_failure(job)
        record_business_event(
            "jobs.job.failed", actor=None, target=job,
            metadata={"job_type": job.job_type, "attempts": job.attempts, "error_class": error_class},
        )
        log.error("job_failed id=%s type=%s attempts=%s class=%s", job.pk, job.job_type, job.attempts, error_class)
        return S.FAILED
    delay = compute_backoff(job.attempts, definition.backoff_base_seconds, definition.backoff_max_seconds)
    job.status = S.RETRY_WAIT
    job.run_after = now + timedelta(seconds=delay)
    job.save()
    log.warning("job_retry id=%s type=%s attempt=%s class=%s in=%ss",
                job.pk, job.job_type, job.attempts, error_class, int(delay))
    return S.RETRY_WAIT


def run_job(job, worker_id):
    """Execute a claimed job. Returns its resulting status."""
    definition = get_job_type(job.job_type)
    if not definition.transactional:
        return _run_non_transactional(job, worker_id, definition)
    try:
        with transaction.atomic():
            locked = Job.objects.select_for_update().get(pk=job.pk)
            if locked.status != S.RUNNING or locked.worker_id != worker_id:
                log.warning("job_lease_lost id=%s worker=%s", job.pk, worker_id)
                return locked.status
            definition.handler(dict(locked.payload), locked)
            locked.status = S.SUCCEEDED
            locked.finished_at = timezone.now()
            locked.lease_expires_at = None
            locked.last_error_class = ""
            locked.last_error = ""
            locked.save()
            return S.SUCCEEDED
    except Exception as error:  # noqa: BLE001 - every handler failure is classified
        error_class, summary = sanitize_error(error)
        with transaction.atomic():
            locked = Job.objects.select_for_update().get(pk=job.pk)
            if locked.status != S.RUNNING or locked.worker_id != worker_id:
                return locked.status
            return _fail_or_retry(locked, error_class, summary, timezone.now(),
                                  permanent=error_class == Job.ErrorClass.PERMANENT)
    except BaseException:
        # Interrupted (Ctrl+C, SystemExit): release the job for another run.
        with transaction.atomic():
            Job.objects.filter(pk=job.pk, status=S.RUNNING, worker_id=worker_id).update(
                status=S.RETRY_WAIT, run_after=timezone.now(), lease_expires_at=None,
                last_error_class=Job.ErrorClass.INTERRUPTED, last_error="Worker interrupted",
            )
        raise


def _still_mine(job_pk, worker_id):
    return Job.objects.filter(pk=job_pk, status=S.RUNNING, worker_id=worker_id).first()


def _run_non_transactional(job, worker_id, definition):
    """Handler runs with no transaction (no lock held during HTTP calls); the
    claim is re-checked before and after. If the worker dies mid-handler, the
    lease expires and the job is recovered (handlers must be idempotent)."""
    current = _still_mine(job.pk, worker_id)
    if current is None:
        return Job.objects.get(pk=job.pk).status
    try:
        definition.handler(dict(current.payload), current)
    except Exception as error:  # noqa: BLE001 - every handler failure is classified
        error_class, summary = sanitize_error(error)
        with transaction.atomic():
            locked = Job.objects.select_for_update().get(pk=job.pk)
            if locked.status != S.RUNNING or locked.worker_id != worker_id:
                return locked.status
            return _fail_or_retry(locked, error_class, summary, timezone.now(),
                                  permanent=error_class == Job.ErrorClass.PERMANENT)
    except BaseException:
        Job.objects.filter(pk=job.pk, status=S.RUNNING, worker_id=worker_id).update(
            status=S.RETRY_WAIT, run_after=timezone.now(), lease_expires_at=None,
            last_error_class=Job.ErrorClass.INTERRUPTED, last_error="Worker interrupted",
        )
        raise
    with transaction.atomic():
        locked = Job.objects.select_for_update().get(pk=job.pk)
        if locked.status != S.RUNNING or locked.worker_id != worker_id:
            log.warning("job_lease_lost id=%s worker=%s", job.pk, worker_id)
            return locked.status
        locked.status = S.SUCCEEDED
        locked.finished_at = timezone.now()
        locked.lease_expires_at = None
        locked.last_error_class = ""
        locked.last_error = ""
        locked.save()
        return S.SUCCEEDED


def recover_expired_leases(now=None, limit=100):
    """Return jobs whose worker died (lease expired, row not locked) to the queue."""
    now = now or timezone.now()
    recovered = 0
    with transaction.atomic():
        jobs = list(
            Job.objects.select_for_update(skip_locked=True)
            .filter(status=S.RUNNING, lease_expires_at__lt=now)
            .order_by("lease_expires_at")[:limit]
        )
        for job in jobs:
            _fail_or_retry(job, Job.ErrorClass.LEASE_EXPIRED, "Lease expired; worker presumed dead", now)
            recovered += 1
    if recovered:
        log.warning("job_leases_recovered count=%s", recovered)
    return recovered


def process_jobs(worker_id, *, max_jobs, max_seconds, clock=time.monotonic):
    """Run up to ``max_jobs`` jobs, claiming none after ``max_seconds``."""
    deadline = clock() + max_seconds
    metrics = {"processed": 0, "succeeded": 0, "retried": 0, "failed": 0}
    while metrics["processed"] < max_jobs and clock() < deadline:
        job = claim_next(worker_id)
        if job is None:
            break
        status = run_job(job, worker_id)
        metrics["processed"] += 1
        if status == S.SUCCEEDED:
            metrics["succeeded"] += 1
        elif status == S.RETRY_WAIT:
            metrics["retried"] += 1
        elif status == S.FAILED:
            metrics["failed"] += 1
    return metrics


# --- Administrative actions -------------------------------------------------------


class JobActionError(ValueError):
    pass


def retry_job(job, *, actor):
    """FAILED or CANCELLED -> PENDING with a fresh attempt budget (audited)."""
    with transaction.atomic():
        job = Job.objects.select_for_update().get(pk=job.pk)
        if job.status not in (S.FAILED, S.CANCELLED):
            raise JobActionError("Only failed or cancelled jobs can be retried.")
        if not get_job_type(job.job_type).admin_retryable:
            raise JobActionError("This job type cannot be retried manually.")
        previous = job.status
        job.status = S.PENDING
        job.attempts = 0
        job.run_after = timezone.now()
        job.finished_at = None
        job.save()
        record_business_event("jobs.job.retried", actor=actor, target=job,
                              metadata={"job_type": job.job_type, "previous_status": previous})
    return job


def cancel_job(job, *, actor):
    """PENDING, RETRY_WAIT or FAILED -> CANCELLED (audited). Running jobs cannot be cancelled."""
    with transaction.atomic():
        job = Job.objects.select_for_update().get(pk=job.pk)
        if job.status not in (S.PENDING, S.RETRY_WAIT, S.FAILED):
            raise JobActionError("Only pending, waiting or failed jobs can be cancelled.")
        previous = job.status
        job.status = S.CANCELLED
        job.finished_at = timezone.now()
        job.save()
        record_business_event("jobs.job.cancelled", actor=actor, target=job,
                              metadata={"job_type": job.job_type, "previous_status": previous})
    return job


# --- Monitoring -----------------------------------------------------------------


def record_heartbeat(worker_id, *, started_at=None, run_seconds=None, metrics=None, cache_ok=None):
    defaults = {"last_seen_at": timezone.now()}
    if started_at is not None:
        defaults["last_started_at"] = started_at
    if run_seconds is not None:
        defaults["last_run_seconds"] = round(run_seconds, 3)
    if metrics is not None:
        defaults.update(last_processed=metrics["processed"], last_succeeded=metrics["succeeded"],
                        last_failed=metrics["retried"] + metrics["failed"])
    if cache_ok is not None:
        defaults.update(cache_ok=cache_ok, cache_checked_at=timezone.now())
    heartbeat, created = WorkerHeartbeat.objects.update_or_create(worker_id=worker_id, defaults=defaults)
    if metrics is not None and metrics["processed"]:
        WorkerHeartbeat.objects.filter(pk=worker_id).update(total_processed=F("total_processed") + metrics["processed"])
    return heartbeat


def queue_health(now=None):
    """Operational snapshot used by the jobs_status command and the admin."""
    now = now or timezone.now()
    counts = dict(Job.objects.values_list("status").annotate(n=Count("id")))
    oldest_due = (Job.objects.filter(status__in=Job.RUNNABLE, run_after__lte=now)
                  .aggregate(oldest=Min("run_after"))["oldest"])
    latest = WorkerHeartbeat.objects.order_by("-last_seen_at").first()
    heartbeat_age = (now - latest.last_seen_at).total_seconds() if latest else None
    cache_heartbeat = WorkerHeartbeat.objects.exclude(cache_checked_at=None).order_by("-cache_checked_at").first()
    failed_24h = Job.objects.filter(status=S.FAILED, finished_at__gte=now - timedelta(hours=24)).count()
    expired_leases = Job.objects.filter(status=S.RUNNING, lease_expires_at__lt=now).count()
    problems = []
    if heartbeat_age is None or heartbeat_age > settings.JOBS_HEARTBEAT_STALE_SECONDS:
        problems.append("worker_heartbeat_stale")
    if oldest_due is not None and (now - oldest_due).total_seconds() > settings.JOBS_BACKLOG_ALERT_SECONDS:
        problems.append("queue_backlog")
    if expired_leases:
        problems.append("expired_leases")
    if cache_heartbeat is not None and cache_heartbeat.cache_ok is False:
        problems.append("cache_unavailable")
    return {
        "counts": {status: counts.get(status, 0) for status in S.values},
        "oldest_due_seconds": round((now - oldest_due).total_seconds()) if oldest_due else 0,
        "heartbeat_age_seconds": round(heartbeat_age) if heartbeat_age is not None else None,
        "failed_last_24h": failed_24h,
        "expired_leases": expired_leases,
        "cache_ok": cache_heartbeat.cache_ok if cache_heartbeat else None,
        "healthy": not problems,
        "problems": problems,
    }
