"""Withdrawal background jobs (Phase 02D runner). DEMO only: no payout is ever dispatched."""

from apps.core.jobs.registry import register
from apps.core.jobs.schedules import Schedule, register_schedule

from . import services


@register("withdrawals.reconcile_payouts", max_attempts=5, backoff_base_seconds=60, backoff_max_seconds=900,
          transactional=False, description="Check pending and uncertain demo payouts (same reference).")
def reconcile_payouts(payload, job):
    services.reconcile_payouts()


@register("withdrawals.housekeeping", max_attempts=2, admin_retryable=True,
          description="Expire stale demo requests and report stuck demo payouts.")
def housekeeping(payload, job):
    services.expire_demo()
    services.detect_stuck()


register_schedule(Schedule("withdrawals.reconcile_payouts", "withdrawals.reconcile_payouts", 300,
                           "Check pending and uncertain demo payouts (same reference)."))
register_schedule(Schedule("withdrawals.housekeeping", "withdrawals.housekeeping", 3600,
                           "Expire stale demo requests and report stuck demo payouts."))
