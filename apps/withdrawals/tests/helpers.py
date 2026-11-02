import uuid

from apps.payments.tests.helpers import PaymentsTestCase, User, make_user
from apps.withdrawals import services
from apps.withdrawals.models import DemoWithdrawal, PayoutBeneficiary


def token():
    return uuid.uuid4().hex


class WithdrawalsTestCase(PaymentsTestCase):
    """Sandbox on. ``root`` creates/executes, ``approver`` and ``third`` review."""

    def setUp(self):
        super().setUp()
        self.third = make_user("third@example.com", role=User.Role.SUPER_ADMIN)

    def mobile(self, member=None, number="0712 345 678", network="MPESA"):
        return services.add_beneficiary(member or self.member, kind="MOBILE_MONEY", network=network,
                                        identifier=number, holder_name="Asha Juma")

    def demo(self, amount=50_000, member=None, beneficiary=None, actor=None):
        member = member or self.member
        if beneficiary is None:
            beneficiary = (PayoutBeneficiary.objects.filter(member=member, status="ACTIVE").first()
                           or self.mobile(member))
        return services.create_demo_withdrawal(actor or self.root, member, beneficiary, amount=amount,
                                               idempotency_key=token())

    def approved(self, amount=50_000):
        demo = self.demo(amount)
        demo = services.start_review(self.approver, demo, expected_status=demo.status)
        demo = services.approve(self.approver, demo, expected_status=demo.status)
        if demo.status != DemoWithdrawal.Status.APPROVED:
            demo = services.approve(self.third, demo, expected_status=demo.status)
        return demo


__all__ = ["DemoWithdrawal", "PayoutBeneficiary", "User", "WithdrawalsTestCase", "make_user", "token"]
