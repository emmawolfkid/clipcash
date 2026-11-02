"""Demo reward background jobs (Phase 02D runner). They never create real transactions."""

from apps.core.jobs.registry import register
from apps.core.jobs.schedules import Schedule, register_schedule

from . import services


@register("rewards.evaluate_pending", max_attempts=3, admin_retryable=True,
          description="Record demo reward decisions for newly verified views.")
def evaluate_pending(payload, job):
    services.evaluate_pending()


@register("rewards.reconcile_reversals", max_attempts=3, admin_retryable=True,
          description="Reverse demo rewards whose views are no longer verified.")
def reconcile_reversals(payload, job):
    services.reconcile_reversals()


register_schedule(Schedule("rewards.evaluate_pending", "rewards.evaluate_pending", 60,
                           "Record demo reward decisions for newly verified views."))
register_schedule(Schedule("rewards.reconcile_reversals", "rewards.reconcile_reversals", 900,
                           "Reverse demo rewards whose views are no longer verified."))
