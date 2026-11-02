"""Payment background jobs (Phase 02D runner). Sandbox only; no real money."""

from apps.core.jobs.registry import register
from apps.core.jobs.schedules import Schedule, register_schedule

from . import services


@register("payments.expire_intents", max_attempts=2, admin_retryable=False,
          description="Expire unpaid payment intents.")
def expire_intents(payload, job):
    services.expire_intents()


@register("payments.poll_pending", max_attempts=5, backoff_base_seconds=60, backoff_max_seconds=900,
          transactional=False, description="Check pending payments with the provider.")
def poll_pending(payload, job):
    services.poll_pending()


@register("payments.check_funding", max_attempts=2, admin_retryable=True,
          description="Verify advertiser sandbox funds against their source records.")
def check_funding(payload, job):
    services.check_funding()


register_schedule(Schedule("payments.expire_intents", "payments.expire_intents", 60, "Expire unpaid payment intents."))
register_schedule(Schedule("payments.poll_pending", "payments.poll_pending", 300,
                           "Check pending payments with the provider."))
register_schedule(Schedule("payments.check_funding", "payments.check_funding", 3600,
                           "Verify advertiser sandbox funds against their source records."))
