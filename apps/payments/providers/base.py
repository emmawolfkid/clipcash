"""
Provider-neutral payment interface. A real provider (e.g. a mobile-money
aggregator) is added later as another implementation; the financial domain in
apps.payments.services only uses this interface.

Trust rules for every provider:
* A callback is only a *notice*: it is authenticated (signature) and then the
  provider's authoritative status is fetched (``fetch_status``). Amounts and
  statuses inside a callback body are never trusted.
* References are normalized and validated before use.
"""

from dataclasses import dataclass


class ProviderError(Exception):
    """Temporary failure talking to the provider (retry with backoff)."""


class CallbackRejected(Exception):
    """The callback is not authentic or not well formed."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ProviderRequest:
    provider_reference: str
    instructions: str


@dataclass(frozen=True)
class ProviderStatus:
    """Authoritative state of one provider transaction."""

    provider_reference: str
    intent_reference: str
    status: str            # PENDING | PAID | FAILED | REFUNDED | CHARGEBACK
    amount: int
    currency: str
    settled: bool
    environment: str
    sequence: int = 0


@dataclass(frozen=True)
class CallbackNotice:
    event_id: str
    provider_reference: str


class PaymentProvider:
    code = ""
    environment = ""

    def create_payment(self, intent):  # -> ProviderRequest
        raise NotImplementedError

    def fetch_status(self, provider_reference):  # -> ProviderStatus | None
        raise NotImplementedError

    def validate_callback(self, body, signature):  # -> CallbackNotice, or raise CallbackRejected
        raise NotImplementedError

    def normalize_reference(self, raw):  # -> str | None
        raise NotImplementedError

    def refund(self, provider_reference, amount, idempotency_key):  # -> refund reference
        raise NotImplementedError
