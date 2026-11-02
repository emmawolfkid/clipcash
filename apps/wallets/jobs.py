"""Wallet background jobs (Phase 02D runner). Read-only checks; never post money."""

from apps.core.jobs.registry import register
from apps.core.jobs.schedules import Schedule, register_schedule

from . import wallet_services


@register("wallets.reconcile", max_attempts=2, admin_retryable=True,
          description="Check member wallets against the ledger.")
def reconcile(payload, job):
    wallet_services.reconcile()


register_schedule(Schedule("wallets.reconcile", "wallets.reconcile", 3600, "Check member wallets against the ledger."))
