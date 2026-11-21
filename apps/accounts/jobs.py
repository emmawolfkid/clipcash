"""Account background jobs (PAY-LIVE-01F-B)."""

from apps.core.jobs.registry import register
from apps.core.jobs.schedules import Schedule, register_schedule

from . import phone_verification


@register("accounts.prune_phone_challenges", max_attempts=2, admin_retryable=False,
          description="Delete closed or long-expired phone verification challenges.")
def prune_phone_challenges(payload, job):
    phone_verification.prune_challenges()


register_schedule(Schedule("accounts.prune_phone_challenges", "accounts.prune_phone_challenges", 86400,
                           "Delete closed or long-expired phone verification challenges."))
