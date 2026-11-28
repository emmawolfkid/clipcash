"""REF-01 background jobs (``manage.py run_jobs`` from cron, like FIN-01). Every job is idempotent:
replays and overlapping runs never qualify, reward, clear or reverse a referral twice (row locks,
one reward per referral, unique ledger idempotency keys). With referral rewards switched off they
only track and qualify; nothing is posted."""

from apps.core.jobs.registry import register
from apps.core.jobs.schedules import Schedule, register_schedule

from . import services


@register("referrals.evaluate", max_attempts=3, admin_retryable=True,
          description="Qualify or expire referrals and retry rewards blocked by a limit or the budget.")
def evaluate(payload, job):
    services.evaluate_due()
    services.retry_allocations()


@register("referrals.clear_rewards", max_attempts=3, admin_retryable=True,
          description="Move pending referral rewards to the available balance after the clearance period.")
def clear_rewards(payload, job):
    services.clear_due()


@register("referrals.reconcile", max_attempts=3, admin_retryable=True,
          description="Reverse referral rewards whose activity was withdrawn and check them against the ledger.")
def reconcile(payload, job):
    services.reconcile()


register_schedule(Schedule("referrals.evaluate", "referrals.evaluate", 900,
                           "Qualify or expire referrals and retry blocked rewards."))
register_schedule(Schedule("referrals.clear_rewards", "referrals.clear_rewards", 300,
                           "Move pending referral rewards to the available balance after clearance."))
register_schedule(Schedule("referrals.reconcile", "referrals.reconcile", 3600,
                           "Reverse withdrawn referral rewards and check them against the ledger."))
