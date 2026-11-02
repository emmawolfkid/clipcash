"""
Staff-only SANDBOX payment simulator. It plays the role of the external
provider: it changes the simulated provider record and then delivers a signed
callback through the same public code path (services.receive_callback). It
never sets ClipCash payment statuses directly, so it cannot bypass verification
or reconciliation. Refused entirely outside sandbox mode.
"""

import json
import secrets

from django.db import transaction

from apps.audit.services import record_business_event

from . import services
from .models import PaymentIntent, SandboxProviderTransaction
from .providers import get_provider

T = SandboxProviderTransaction.Status
ACTIONS = ("pay", "pay_wrong_amount", "fail", "settle", "chargeback", "replay", "orphan", "forged")


def _deliver(provider, reference, sequence, *, signature=None):
    body = json.dumps({"event_id": f"{reference}:{sequence}", "provider_reference": reference},
                      separators=(",", ":")).encode()
    return services.receive_callback(provider.code, body, signature or provider.sign(body))


def simulate(actor, intent, action):
    """Returns (http_status, outcome) of the delivered callback."""
    services.require_sandbox()
    services._require(services.can_operate_sandbox, actor, "sandbox.simulate", intent)
    if action not in ACTIONS:
        raise services.InvalidState("Unknown simulator action.")
    provider = get_provider(intent.provider)
    intent = PaymentIntent.objects.get(pk=intent.pk)
    with transaction.atomic():
        record_business_event("payments.sandbox.simulated", actor=actor, target=intent,
                              metadata={"reference": intent.reference, "action": action})
        if action == "orphan":
            reference = "SBX-" + secrets.token_hex(8).upper()
            SandboxProviderTransaction.objects.create(provider_reference=reference, intent_reference="CC-UNKNOWN",
                                                      amount=intent.amount, status=T.PAID, sequence=1)
            sequence = 1
        else:
            record = SandboxProviderTransaction.objects.select_for_update().get(
                provider_reference=intent.provider_reference)
            reference = record.provider_reference
            if action != "replay" and action != "forged":
                if action == "pay":
                    record.status, record.amount = T.PAID, intent.amount
                elif action == "pay_wrong_amount":
                    record.status, record.amount = T.PAID, intent.amount + 1_000
                elif action == "fail":
                    record.status = T.FAILED
                elif action == "settle":
                    if record.status != T.PAID:
                        raise services.InvalidState("Only paid sandbox transactions can settle.")
                    record.settled = True
                elif action == "chargeback":
                    if not record.settled:
                        raise services.InvalidState("Only settled sandbox transactions can be charged back.")
                    record.status = T.CHARGEBACK
                record.sequence += 1
                record.save()
            sequence = record.sequence
    if action == "forged":
        return _deliver(provider, reference, sequence + 1, signature="0" * 64)
    return _deliver(provider, reference, sequence)
