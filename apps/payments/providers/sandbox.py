"""
SANDBOX provider: a local simulation of an external payment provider. Its
"external" records live in SandboxProviderTransaction and change only through
the staff sandbox simulator (apps.payments.simulator). No network, no real
money, no real credentials. Callback signatures use an HMAC key derived from
SECRET_KEY, so forged notices are refused.
"""

import hmac
import json
import re
import secrets

from django.utils.crypto import salted_hmac

from .base import CallbackNotice, CallbackRejected, PaymentProvider, ProviderRequest, ProviderStatus

REFERENCE_RE = re.compile(r"^SBX-[0-9A-F]{16}$")
EVENT_RE = re.compile(r"^[A-Za-z0-9:._-]{1,100}$")
KEY_SALT = "clipcash.payments.sandbox.callback"


class SandboxProvider(PaymentProvider):
    code = "sandbox"
    environment = "SANDBOX"

    def __repr__(self):
        return "<SandboxProvider>"

    @staticmethod
    def sign(body):
        return salted_hmac(KEY_SALT, body, algorithm="sha256").hexdigest()

    def create_payment(self, intent):
        from apps.payments.models import SandboxProviderTransaction

        reference = "SBX-" + secrets.token_hex(8).upper()
        SandboxProviderTransaction.objects.create(provider_reference=reference, intent_reference=intent.reference,
                                                  amount=intent.amount, currency=intent.currency)
        return ProviderRequest(reference, "Sandbox payment: ClipCash staff simulate the provider. No money moves.")

    def fetch_status(self, provider_reference):
        from apps.payments.models import SandboxProviderTransaction

        record = SandboxProviderTransaction.objects.filter(provider_reference=provider_reference).first()
        if record is None:
            return None
        return ProviderStatus(provider_reference=record.provider_reference, intent_reference=record.intent_reference,
                              status=record.status, amount=record.amount, currency=record.currency,
                              settled=record.settled, environment=self.environment, sequence=record.sequence)

    def normalize_reference(self, raw):
        value = str(raw or "").strip().upper()
        return value if REFERENCE_RE.match(value) else None

    def validate_callback(self, body, signature):
        if not isinstance(body, bytes) or not signature or not isinstance(signature, str):
            raise CallbackRejected("signature_missing")
        if not hmac.compare_digest(self.sign(body), signature.strip().lower()):
            raise CallbackRejected("signature_invalid")
        try:
            data = json.loads(body)
        except ValueError:
            raise CallbackRejected("malformed") from None
        if not isinstance(data, dict) or set(data) != {"event_id", "provider_reference"}:
            raise CallbackRejected("malformed")
        event_id = data["event_id"]
        if not isinstance(event_id, str) or not EVENT_RE.match(event_id):
            raise CallbackRejected("malformed")
        reference = self.normalize_reference(data["provider_reference"])
        if reference is None:
            raise CallbackRejected("bad_reference")
        return CallbackNotice(event_id=event_id, provider_reference=reference)

    def refund(self, provider_reference, amount, idempotency_key):
        from apps.payments.models import SandboxProviderTransaction

        record = SandboxProviderTransaction.objects.select_for_update().get(provider_reference=provider_reference)
        if record.status == SandboxProviderTransaction.Status.PAID and record.amount == amount:
            record.status = SandboxProviderTransaction.Status.REFUNDED
            record.sequence += 1
            record.save()
        return "SBXR-" + provider_reference[4:]
