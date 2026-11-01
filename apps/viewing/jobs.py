"""Viewing background jobs (Phase 02D runner)."""

from apps.core.jobs.registry import register
from apps.core.jobs.schedules import Schedule, register_schedule

from . import services


@register("viewing.expire_sessions", max_attempts=2, admin_retryable=False,
          description="Expire unstarted watch sessions and close idle ones.")
def expire_sessions(payload, job):
    services.expire_sessions()


register_schedule(Schedule("viewing.expire_sessions", "viewing.expire_sessions", 60,
                           "Expire unstarted watch sessions and close idle ones."))
