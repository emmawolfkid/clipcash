import uuid
from contextlib import contextmanager
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase

from apps.wallets import services
from apps.wallets.services import credit, debit

User = get_user_model()
PASSWORD = "Correct-Horse-Battery-9"


@contextmanager
def postings_enabled():
    """Tests only: open the financial-feature gate so posting logic can be
    exercised. Production code has no equivalent; the flags stay locked."""
    with mock.patch("apps.wallets.services._feature_enabled", return_value=True):
        yield


def key():
    return f"test-{uuid.uuid4().hex}"


def check_deferred_now():
    """Run commit-time ledger checks immediately (TestCase never commits)."""
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute("SET CONSTRAINTS ALL DEFERRED")


class LedgerFixtures:
    """Builds a funded campaign through the controlled posting types."""

    def make_people(self):
        self.root = User.objects.create_superuser("root@example.com", PASSWORD)
        self.verifier = User.objects.create_superuser("verifier@example.com", PASSWORD)

    def make_accounts(self):
        self.advertiser_id = str(uuid.uuid4())
        self.campaign_id = str(uuid.uuid4())
        self.member_id = str(uuid.uuid4())
        self.clearing = services.get_platform_account("PAYMENT_CLEARING")
        self.cash = services.get_platform_account("CASH")
        self.deferred = services.get_platform_account("MEMBERSHIP_DEFERRED_REVENUE")
        self.membership_revenue = services.get_platform_account("MEMBERSHIP_REVENUE")
        self.withdrawal_clearing = services.get_platform_account("WITHDRAWAL_CLEARING")
        self.pending = services.open_account("ADVERTISER_FUNDING_PENDING", scope_id=self.advertiser_id)
        self.verified = services.open_account("ADVERTISER_FUNDING_VERIFIED", scope_id=self.advertiser_id)
        self.campaign = services.open_account("CAMPAIGN_RESTRICTED_FUNDING", scope_id=self.campaign_id)
        self.rewards = services.open_account("REWARD_LIABILITY", scope_id=self.member_id)

    def receive(self, amount, reference=None):
        return services.post_entry(
            "advertiser.funding_received",
            lines=[debit(self.clearing, amount), credit(self.pending, amount)],
            idempotency_key=key(), source_type="test", description="Test receipt",
            external_reference=reference or f"PROV-{uuid.uuid4().hex[:12]}",
        )

    def verify(self, receipt, actor=None, amount=None):
        amount = amount if amount is not None else receipt.total_amount
        return services.post_entry(
            "advertiser.funding_verified",
            lines=[debit(self.pending, amount), credit(self.verified, amount)],
            idempotency_key=key(), source_type="test", description="Test verification",
            related_entry=receipt, actor=actor or self.verifier,
        )

    def commit(self, amount):
        return services.post_entry(
            "campaign.funding_committed",
            lines=[debit(self.verified, amount), credit(self.campaign, amount)],
            idempotency_key=key(), source_type="test", description="Test commitment", actor=self.root,
        )

    def allocate(self, amount, idempotency_key=None):
        return services.post_entry(
            "reward.allocated",
            lines=[debit(self.campaign, amount), credit(self.rewards, amount)],
            idempotency_key=idempotency_key or key(), source_type="test", description="Test reward",
        )

    def fund_campaign(self, amount):
        self.commit(self.verify(self.receive(amount)).total_amount)

    def refresh(self, *accounts):
        for account in accounts:
            account.refresh_from_db()


class LedgerTestCase(LedgerFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.make_people()
        self.make_accounts()
