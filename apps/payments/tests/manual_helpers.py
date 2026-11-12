"""PAY-01 test helpers: a production-like configuration with manual (Airtel Lipa)
membership payments switched on, sandbox payments off and no restricted demo."""

import uuid

from django.contrib.auth.models import Permission
from django.test import override_settings

from apps.accounts.tests.helpers import User, make_user
from apps.memberships import services as memberships
from apps.memberships.tests.helpers import MembershipTestCase
from apps.payments import manual

MANUAL_ON = override_settings(MANUAL_MEMBERSHIP_PAYMENTS_ENABLED=True, RESTRICTED_DEMO_MODE=False,
                              PAYMENTS_MODE="disabled", PAYMENTS_SANDBOX_ALLOWED=False,
                              PUBLIC_REGISTRATION_APPROVED=True, MEMBERSHIPS_COMPLIMENTARY_GRANTS_ENABLED=False)
BUSINESS_NUMBER = "123456"  # Test value only; real details are entered by a Super Administrator.
RECIPIENT = "CLIPCASH TEST RECIPIENT"


def airtel_reference():
    return "MP" + uuid.uuid4().hex[:10].upper()


def staff_with(email, *codenames):
    user = make_user(email, role=User.Role.CONTENT_MANAGER)
    for codename in codenames:
        user.user_permissions.add(Permission.objects.get(content_type__app_label="payments", codename=codename))
    return User.objects.get(pk=user.pk)  # Fresh instance: no stale permission cache.


@MANUAL_ON
class ManualPaymentTestCase(MembershipTestCase):
    """``root`` Super Administrator, ``member`` with a ``plan`` (TZS 5,000 / 30 days),
    ``verifier`` staff holding payments.verify_manual_payments, published Lipa details."""

    def setUp(self):
        super().setUp()
        self.verifier = staff_with("verifier@example.com", "verify_manual_payments")
        self.instruction = manual.publish_instruction(self.root, business_number=BUSINESS_NUMBER,
                                                      recipient_name=RECIPIENT, instructions="Dial *150*60#.")

    def choose(self, member=None, plan=None):
        return memberships.choose_plan(member or self.member, plan or self.plan)

    def submit(self, subscription=None, member=None, reference=None, amount=None, phone="0781234567", **extra):
        subscription = subscription or self.choose(member)
        claim, _created = manual.submit_claim(
            member or subscription.member, subscription,
            amount=subscription.price_amount if amount is None else amount, payer_phone=phone,
            transaction_reference=reference or airtel_reference(), **extra)
        return claim

    def approve(self, claim, actor=None, amount=None, reference=None, confirmed=True):
        return manual.approve(actor or self.verifier, claim,
                              verified_amount=claim.amount if amount is None else amount,
                              verified_reference=reference or claim.transaction_reference,
                              receipt_confirmed=confirmed, recipient_confirmed=confirmed)
