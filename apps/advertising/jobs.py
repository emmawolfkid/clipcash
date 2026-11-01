"""Advertising background jobs (Phase 02D runner)."""

from apps.core.jobs.registry import register
from apps.core.jobs.schedules import Schedule, register_schedule

from . import services


@register("advertising.campaign_schedule", max_attempts=2, admin_retryable=False,
          description="Start due campaigns and complete ended ones.")
def campaign_schedule(payload, job):
    services.run_schedule()


register_schedule(Schedule("advertising.campaign_schedule", "advertising.campaign_schedule", 60,
                           "Start due campaigns and complete ended ones."))
