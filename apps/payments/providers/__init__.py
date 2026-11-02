from django.conf import settings

from .base import CallbackRejected, ProviderError, ProviderStatus  # noqa: F401
from .sandbox import SandboxProvider


class PaymentsDisabled(Exception):
    """Payments are off, or sandbox simulation is not allowed in this environment."""


def sandbox_active():
    """Hard server-side gate: sandbox only when configured AND in DEBUG or an
    explicitly isolated sandbox deployment. There is no live mode."""
    return settings.PAYMENTS_MODE == "sandbox" and bool(settings.DEBUG or settings.PAYMENTS_SANDBOX_ALLOWED)


def get_provider(code="sandbox"):
    if not sandbox_active():
        raise PaymentsDisabled("Payments are not available in this environment.")
    if code != SandboxProvider.code:
        raise PaymentsDisabled(f"Unknown payment provider {code!r}.")
    return SandboxProvider()
