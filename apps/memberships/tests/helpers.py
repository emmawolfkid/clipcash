import uuid
from contextlib import contextmanager
from unittest import mock

from django.core.cache import cache
from django.utils import timezone

from apps.accounts.tests.helpers import PASSWORD, AccountsTestCase, User, make_user
from apps.core.models import SiteConfiguration
from apps.memberships import services
from apps.memberships.services import VerifiedPayment


def enable_memberships(enabled=True):
    SiteConfiguration.objects.update_or_create(id=1, defaults={"memberships_enabled": enabled})
    cache.clear()


@contextmanager
def payments_enabled(verifier_result=True):
    """Tests only: open the live_payments gate and register a fake verifier.
    Production has neither: payments stay locked and no verifier exists."""
    real = services._feature_enabled
    with mock.patch("apps.memberships.services._feature_enabled",
                    side_effect=lambda key: True if key == "live_payments" else real(key)):
        services.register_payment_verifier("test", lambda payment: verifier_result)
        try:
            yield
        finally:
            services.PAYMENT_VERIFIERS.pop("test", None)


def payment_for(subscription, amount=None, reference=None):
    return VerifiedPayment(verifier="test", reference=reference or f"PAY-{uuid.uuid4().hex[:10]}",
                           amount=subscription.price_amount if amount is None else amount,
                           currency="TZS", confirmed_at=timezone.now())


class MembershipTestCase(AccountsTestCase):
    def setUp(self):
        super().setUp()
        enable_memberships()
        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.member = make_user("member@example.com")
        self.plan = self.make_plan("monthly", price=5_000, days=30)

    def make_plan(self, code, price=5_000, days=30, **extra):
        values = {"name": code.title(), "price_amount": price, "duration_days": days,
                  "benefits": ["Ad-supported library"], "entitlements": ["videos.standard"], **extra}
        return services.create_plan(self.root, code=code, **values)

    def activate(self, subscription, **kwargs):
        with payments_enabled():
            return services.activate_with_verified_payment(subscription.pk, payment_for(subscription, **kwargs))

    def active_membership(self, member=None, plan=None):
        return self.activate(services.choose_plan(member or self.member, plan or self.plan))


__all__ = ["PASSWORD", "User", "make_user", "MembershipTestCase", "enable_memberships", "payments_enabled",
           "payment_for"]
