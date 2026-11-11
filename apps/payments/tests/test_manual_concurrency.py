"""PAY-01: concurrent approvals and submissions on real PostgreSQL connections (one
per thread). Each scenario must leave exactly one claim, one ledger entry and one
activated membership."""

from unittest import skipUnless

from django.db import connection
from django.test import TransactionTestCase

from apps.accounts.tests.helpers import User, make_user
from apps.core.models import SiteConfiguration
from apps.memberships import services as memberships
from apps.memberships.models import Subscription
from apps.memberships.tests.test_concurrency import run_threads
from apps.payments import manual
from apps.payments.models import ManualMembershipPayment
from apps.wallets import services as ledger
from apps.wallets.models import JournalEntry

from .manual_helpers import MANUAL_ON, staff_with


@MANUAL_ON
@skipUnless(connection.vendor == "postgresql", "Concurrency is verified on PostgreSQL.")
class ManualPaymentConcurrencyTests(TransactionTestCase):
    def setUp(self):
        import importlib

        from django.apps import apps
        from django.core.cache import cache

        # TransactionTestCase flushes tables; restore the chart of accounts (as wallets tests do).
        importlib.import_module("apps.wallets.migrations.0003_platform_accounts").create_platform_accounts(apps, None)
        SiteConfiguration.objects.update_or_create(id=1, defaults={"memberships_enabled": True})
        cache.clear()
        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.member = make_user("member@example.com")
        self.verifiers = [staff_with(f"verifier{i}@example.com", "verify_manual_payments") for i in range(2)]
        self.plan = memberships.create_plan(self.root, code="monthly", name="Monthly", price_amount=5_000,
                                            duration_days=30, entitlements=["videos.standard"])
        manual.publish_instruction(self.root, business_number="123456", recipient_name="ClipCash Test")

    def submit(self, member, reference):
        pending = memberships.choose_plan(member, self.plan)
        return manual.submit_claim(member, pending, amount=5_000, payer_phone="0781234567",
                                   transaction_reference=reference)[0]

    def test_concurrent_approvals_activate_once(self):
        claim = self.submit(self.member, "MP80808080")
        actors = self.verifiers + [self.root, self.verifiers[0]]
        results = run_threads(len(actors), lambda i: manual.approve(
            actors[i], claim, verified_amount=5_000, verified_reference="MP80808080", receipt_confirmed=True))
        self.assertTrue(all(isinstance(result, tuple) for result in results), results)
        self.assertEqual(sum(1 for _claim, changed in results if changed), 1)
        self.assertEqual(JournalEntry.objects.count(), 1)
        self.assertEqual(Subscription.objects.filter(member=self.member, status="ACTIVE").count(), 1)
        self.assertEqual(ledger.get_platform_account("CASH").balance, 5_000)

    def test_concurrent_approve_and_reject_decide_once(self):
        claim = self.submit(self.member, "MP81818181")

        def act(index):
            if index % 2:
                return manual.reject(self.verifiers[1], claim, reason="NOT_FOUND")
            return manual.approve(self.verifiers[0], claim, verified_amount=5_000, verified_reference="MP81818181",
                                  receipt_confirmed=True)

        results = run_threads(4, act)
        claim.refresh_from_db()
        self.assertIn(claim.status, ("APPROVED", "REJECTED"))
        winners = [r for r in results if isinstance(r, tuple) and r[1]]
        self.assertEqual(len(winners), 1, results)
        expected_entries = 1 if claim.status == "APPROVED" else 0
        self.assertEqual(JournalEntry.objects.count(), expected_entries)
        self.assertEqual(Subscription.objects.filter(member=self.member, status="ACTIVE").count(), expected_entries)

    def test_same_reference_from_many_members_is_accepted_once(self):
        members = [make_user(f"m{i}@example.com") for i in range(4)]
        pendings = [memberships.choose_plan(member, self.plan) for member in members]
        results = run_threads(4, lambda i: manual.submit_claim(
            members[i], pendings[i], amount=5_000, payer_phone="0781234567", transaction_reference="MP90909090"))
        self.assertEqual(sum(1 for result in results if isinstance(result, tuple)), 1, results)
        self.assertEqual(ManualMembershipPayment.objects.filter(reference_key="MP90909090").count(), 1)

    def test_double_submission_by_one_member_creates_one_claim(self):
        pending = memberships.choose_plan(self.member, self.plan)
        results = run_threads(4, lambda i: manual.submit_claim(
            self.member, pending, amount=5_000, payer_phone="0781234567", transaction_reference="MP70707070"))
        self.assertTrue(all(isinstance(result, tuple) for result in results), results)
        self.assertEqual(len({claim.pk for claim, _created in results}), 1)
        self.assertEqual(sum(1 for _claim, created in results if created), 1)
        self.assertEqual(ManualMembershipPayment.objects.count(), 1)
