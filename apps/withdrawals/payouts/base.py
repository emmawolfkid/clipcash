"""
Provider-neutral payout interface. A real mobile-money or bank payout provider
is added later as another implementation; apps.withdrawals.services only uses
this interface.

Rules for every provider:
* A callback is only a notice: authenticate it, then fetch the authoritative
  status. Amounts or statuses in a callback body are never trusted.
* A timeout or outage is NOT a failure. The payout's status becomes uncertain
  and is reconciled with the SAME reference; it is never re-sent with a new one.
"""

from dataclasses import dataclass


class PayoutTimeout(Exception):
    """The provider did not answer: outcome unknown."""


class CallbackRejected(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PayoutStatus:
    reference: str
    withdrawal_reference: str
    status: str  # PENDING | SUCCEEDED | FAILED | RETURNED
    amount: int
    sequence: int = 0


@dataclass(frozen=True)
class PayoutNotice:
    event_id: str
    reference: str


class PayoutProvider:
    code = ""

    def validate_beneficiary(self, beneficiary):  # -> (ok: bool, reason: str)
        raise NotImplementedError

    def create_payout(self, reference, withdrawal_reference, amount):  # raises PayoutTimeout
        raise NotImplementedError

    def fetch_status(self, reference):  # -> PayoutStatus | None; raises PayoutTimeout
        raise NotImplementedError

    def validate_callback(self, body, signature):  # -> PayoutNotice; raises CallbackRejected
        raise NotImplementedError

    def normalize_reference(self, raw):  # -> str | None
        raise NotImplementedError
