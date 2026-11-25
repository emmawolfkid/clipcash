"""Notification jobs and operational alerts (Phase 15-A)."""

from django.utils.translation import gettext_noop
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.core.jobs.registry import register
from apps.core.jobs.schedules import Schedule, register_schedule
from apps.core.jobs.services import job_failed

from . import inbox
from .models import Notification


@register("notifications.prune_notifications", max_attempts=2, admin_retryable=False,
          description="Delete read in-app notifications past retention.")
def prune_notifications(payload, job):
    cutoff = timezone.now() - timedelta(days=settings.NOTIFICATIONS_RETENTION_DAYS)
    Notification.objects.filter(read_at__isnull=False, created_at__lt=cutoff).delete()


register_schedule(Schedule("notifications.prune_notifications", "notifications.prune_notifications", 86400,
                           "Delete read in-app notifications past retention."))


def alert_operators_of_failed_job(sender, job, **kwargs):
    """A background job failed for good: tell the people who can see and retry jobs.
    Once per job; the text names the job type only (never its payload or error text)."""
    inbox.notify(
        inbox.staff_with_permission("core.view_operations"), "operations.job.failed",
        category=inbox.Category.OPERATIONS, title=gettext_noop("A background job failed"),
        message=gettext_noop("Job type {job_type} failed after {attempts} attempt(s). Check Operational health."),
        params={"job_type": job.job_type, "attempts": job.attempts},
        link=inbox.link_to("administration:health"), dedupe_key=f"job.failed:{job.pk}")


job_failed.connect(alert_operators_of_failed_job, dispatch_uid="notifications.alert_failed_job")
