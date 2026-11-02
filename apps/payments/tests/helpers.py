import json
import uuid

from django.test import override_settings

from apps.advertising.tests.helpers import AdvertisingTestCase, User, make_user
from apps.memberships import services as memberships
from apps.payments import services, simulator
from apps.payments.models import PaymentIntent, SandboxPosting, SandboxProviderTransaction
from apps.payments.providers import SandboxProvider

SANDBOX = override_settings(PAYMENTS_MODE="sandbox", PAYMENTS_SANDBOX_ALLOWED=True)


def token():
    return uuid.uuid4().hex


def signed_body(reference, sequence):
    body = json.dumps({"event_id": f"{reference}:{sequence}", "provider_reference": reference},
                      separators=(",", ":")).encode()
    return body, SandboxProvider.sign(body)


@SANDBOX
class PaymentsTestCase(AdvertisingTestCase):
    """``root`` and ``approver`` are Super Administrators; ``owner`` runs the approved ``org``."""

    def setUp(self):
        super().setUp()
        self.approver = make_user("approver@example.com", role=User.Role.SUPER_ADMIN)

    def deposit(self, amount=50_000, org=None, user=None):
        return services.create_deposit_intent(user or self.owner, org or self.org, amount=amount,
                                              idempotency_key=token())

    def settled_deposit(self, amount=50_000):
        intent = self.deposit(amount)
        simulator.simulate(self.root, intent, "pay")
        simulator.simulate(self.root, intent, "settle")
        intent.refresh_from_db()
        assert intent.status == PaymentIntent.Status.SETTLED, intent.status
        return intent

    def membership_intent(self, member=None):
        member = member or self.member
        subscription = memberships.choose_plan(member, self.plan)
        return services.create_membership_intent(member, subscription, idempotency_key=token())

    def balances_total(self):
        return sum(services.sandbox_balances().values())

    def provider_record(self, intent):
        return SandboxProviderTransaction.objects.get(provider_reference=intent.provider_reference)


__all__ = ["PaymentIntent", "PaymentsTestCase", "SANDBOX", "SandboxPosting", "User", "make_user", "signed_body",
           "token"]
