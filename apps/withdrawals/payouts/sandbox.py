"""
SANDBOX payout provider: a local simulation. No network, no money, no real
credentials. Its records (SandboxPayout) change only through the staff
simulator; callbacks are HMAC-signed with a key derived from SECRET_KEY.
"""

import hmac
import json
import re

from django.utils.crypto import salted_hmac

from .base import CallbackRejected, PayoutNotice, PayoutProvider, PayoutStatus, PayoutTimeout

REFERENCE_RE = re.compile(r"^DPO-[0-9A-F]{16}$")
EVENT_RE = re.compile(r"^[A-Za-z0-9:._-]{1,100}$")
KEY_SALT = "clipcash.withdrawals.sandbox.callback"


class SandboxPayoutProvider(PayoutProvider):
    code = "sandbox"

    def __repr__(self):
        return "<SandboxPayoutProvider>"

    @staticmethod
    def sign(body):
        return salted_hmac(KEY_SALT, body, algorithm="sha256").hexdigest()

    def validate_beneficiary(self, beneficiary):
        # Demo check only: format already validated; no real account lookup happens.
        return True, "demo_check_passed"

    def create_payout(self, reference, withdrawal_reference, amount):
        from apps.withdrawals.models import SandboxPayout

        record, _ = SandboxPayout.objects.get_or_create(reference=reference, defaults={
            "withdrawal_reference": withdrawal_reference, "amount": amount})
        if record.unreachable:
            raise PayoutTimeout("Sandbox provider unreachable.")
        return record.reference

    def fetch_status(self, reference):
        from apps.withdrawals.models import SandboxPayout

        record = SandboxPayout.objects.filter(reference=reference).first()
        if record is None:
            return None
        if record.unreachable:
            raise PayoutTimeout("Sandbox provider unreachable.")
        return PayoutStatus(reference=record.reference, withdrawal_reference=record.withdrawal_reference,
                            status=record.status, amount=record.amount, sequence=record.sequence)

    def normalize_reference(self, raw):
        value = str(raw or "").strip().upper()
        return value if REFERENCE_RE.match(value) else None

    def validate_callback(self, body, signature):
        if not isinstance(body, bytes) or not isinstance(signature, str) or not signature:
            raise CallbackRejected("signature_missing")
        if not hmac.compare_digest(self.sign(body), signature.strip().lower()):
            raise CallbackRejected("signature_invalid")
        try:
            data = json.loads(body)
        except ValueError:
            raise CallbackRejected("malformed") from None
        if not isinstance(data, dict) or set(data) != {"event_id", "reference"}:
            raise CallbackRejected("malformed")
        if not isinstance(data["event_id"], str) or not EVENT_RE.match(data["event_id"]):
            raise CallbackRejected("malformed")
        reference = self.normalize_reference(data["reference"])
        if reference is None:
            raise CallbackRejected("bad_reference")
        return PayoutNotice(event_id=data["event_id"], reference=reference)
