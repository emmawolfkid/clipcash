from apps.payments.providers import PaymentsDisabled, sandbox_active

from .base import CallbackRejected, PayoutTimeout  # noqa: F401
from .sandbox import SandboxPayoutProvider


def get_payout_provider(code="sandbox"):
    """Same hard gate as sandbox payments: simulation only in DEBUG or an isolated
    demo deployment. There is no live payout provider."""
    if not sandbox_active():
        raise PaymentsDisabled("Payouts are not available in this environment.")
    if code != SandboxPayoutProvider.code:
        raise PaymentsDisabled(f"Unknown payout provider {code!r}.")
    return SandboxPayoutProvider()
