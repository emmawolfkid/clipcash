"""FIN-01 background jobs (Phase 02D runner: cron runs ``manage.py run_jobs`` every minute; no
persistent worker is needed, which suits NetPoa shared hosting). Every job is idempotent: replays
and overlapping runs never settle, clear or reverse a reward twice (row locks, one reward per
session, unique ledger idempotency keys). With real-money rewards switched off they post nothing."""

from apps.core.jobs.registry import register
from apps.core.jobs.schedules import Schedule, register_schedule

from . import services


@register("earnings.settle_rewards", max_attempts=3, admin_retryable=True,
          description="Issue or release real rewards for closed viewing sessions.")
def settle_rewards(payload, job):
    services.settle_due()


@register("earnings.clear_rewards", max_attempts=3, admin_retryable=True,
          description="Move pending real rewards to the available balance after the clearance period.")
def clear_rewards(payload, job):
    services.clear_due()


@register("earnings.reconcile", max_attempts=3, admin_retryable=True,
          description="Retry deferred reward reversals and check rewards against the ledger.")
def reconcile(payload, job):
    services.reconcile()


register_schedule(Schedule("earnings.settle_rewards", "earnings.settle_rewards", 60,
                           "Issue or release real rewards for closed viewing sessions."))
register_schedule(Schedule("earnings.clear_rewards", "earnings.clear_rewards", 300,
                           "Move pending real rewards to the available balance after clearance."))
register_schedule(Schedule("earnings.reconcile", "earnings.reconcile", 3600,
                           "Retry deferred reward reversals and check rewards against the ledger."))
