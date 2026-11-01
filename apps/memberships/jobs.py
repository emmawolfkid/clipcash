"""Membership background jobs (Phase 02D infrastructure)."""

from apps.core.jobs.registry import register
from apps.core.jobs.schedules import Schedule, register_schedule

from . import services


@register("memberships.process_expiry", max_attempts=3, admin_retryable=False,
          description="Expire ended memberships and cancel unpaid choices.")
def process_expiry(payload, job):
    services.process_expiry()


register_schedule(Schedule("memberships.process_expiry", "memberships.process_expiry", 900,
                           "Expire ended memberships and cancel unpaid choices."))
