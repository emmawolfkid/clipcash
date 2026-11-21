"""
PAY-LIVE-01F-B: provider-neutral SMS delivery.

A transport has ``available`` and ``send(to, body)``. Only two exist:

* ``disabled`` (the default, and what production uses until a real provider is chosen):
  not available; sending raises SmsUnavailable.
* ``fake``: keeps messages in ``outbox`` for automated tests and local development. It is
  refused unless settings.SMS_FAKE_TRANSPORT_ALLOWED (DEBUG; no environment variable), so a
  production configuration can never turn it on, and check --deploy reports it (accounts.E009).

A real Tanzanian provider is added later as a third transport (credentials from the
environment, an HTTPS API call with a timeout, no redirects, delivery errors mapped to
SmsDeliveryError). Message bodies may contain one-time codes: transports must never log them.
"""

from django.conf import settings

BACKENDS = ("disabled", "fake")

# The fake transport's sent messages: [(to, body)]. Tests clear it; nothing else reads it.
outbox = []


class SmsUnavailable(Exception):
    """No SMS transport is configured: nothing was or can be sent."""


class SmsDeliveryError(Exception):
    """The transport could not deliver the message (temporary or permanent)."""


class DisabledTransport:
    name = "disabled"
    available = False

    def send(self, to, body):
        raise SmsUnavailable("SMS delivery is not available.")


class FakeTransport:
    """In-memory transport for tests and local development only. Never logs the body."""

    name = "fake"
    available = True

    def send(self, to, body):
        outbox.append((to, body))


def get_transport():
    if settings.SMS_BACKEND == "fake" and settings.SMS_FAKE_TRANSPORT_ALLOWED is True:
        return FakeTransport()
    return DisabledTransport()  # "disabled", an unknown name, or "fake" outside DEBUG.


def delivery_available():
    return get_transport().available
