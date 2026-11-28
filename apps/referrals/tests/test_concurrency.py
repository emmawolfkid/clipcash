"""REF-01 concurrency on the real database (MariaDB here). Real threads and row locks: two reviewers
approving at once, or two referrals of one referrer racing for a daily limit of one, never produce a
second reward, a second ledger entry or an over-commitment of the budget."""

import importlib
from datetime import timedelta
from unittest import mock

from django.apps import apps
from django.core.cache import cache
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from apps.accounts import services as accounts
from apps.accounts.tests.helpers import PASSWORD, User, make_user
from apps.earnings.tests.helpers import reference, staff
from apps.memberships.tests.test_concurrency import run_threads
from apps.referrals import funding, services
from apps.referrals.models import Referral, ReferralReward
from apps.viewing.tests.helpers import Clock
from apps.wallets.models import JournalEntry

ON = override_settings(REFERRAL_REWARDS_ENABLED=True, REFERRAL_REWARDS_APPROVED=True, RESTRICTED_DEMO_MODE=False,
                       PAYMENTS_MODE="disabled")


@ON
class ReferralConcurrencyTests(TransactionTestCase):
    def setUp(self):
        # TransactionTestCase flushes tables: restore the platform ledger accounts (wallets 0003, 0008).
        importlib.import_module("apps.wallets.migrations.0003_platform_accounts").create_platform_accounts(apps, None)
        importlib.import_module("apps.wallets.migrations.0008_referral_accounts").create_accounts(apps, None)
        cache.clear()
        self.clock = Clock()
        patcher = mock.patch("django.utils.timezone.now", side_effect=lambda: self.clock.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.proposer = staff("p@x.example", "referrals.manage_referral_policy")
        self.approver = staff("a@x.example", "referrals.approve_referral_policy")
        self.reviewers = [staff(f"r{i}@x.example", "referrals.review_referrals") for i in range(2)]
        recorder = staff("mk@x.example", "referrals.record_marketing_deposits", "referrals.allocate_referral_budget")
        verifier = staff("mv@x.example", "referrals.verify_marketing_deposits")
        current = services.current_policy()
        values = {name: getattr(current, name) for name in services.POLICY_FIELDS}
        values.update({"rewards_open": True, "reward_amount": 1000, "referrer_requires_membership": False,
                       "daily_reward_limit": 1, "lifetime_reward_limit": ""})
        proposal = services.propose_policy(self.proposer, expected_version=1, reason="Concurrency", **values)
        services.decide_policy(self.approver, proposal, approve=True)
        deposit = funding.record_deposit(recorder, method="AIRTEL_LIPA", amount=5000, external_reference=reference(),
                                         paid_on=timezone.localdate())
        funding.verify_deposit(verifier, deposit, receipt_confirmed=True)
        funding.commit_budget(recorder, amount=5000)
        self.referrer = make_user("referrer@x.example")
        self.code = services.ensure_code(self.referrer)

    def under_review(self, index):
        user = accounts.register_member(contact_method="email", contact=f"new{index}@x.example", password=PASSWORD,
                                        first_name="New", last_name="Member", referral_code=self.code.code)
        referral = Referral.objects.get(referred=user)
        Referral.objects.filter(pk=referral.pk).update(status=Referral.Status.UNDER_REVIEW,
                                                       qualified_at=self.clock.now)
        return referral

    def test_two_reviewers_approving_at_once_allocate_once(self):
        referral = self.under_review(1)
        results = run_threads(2, lambda i: services.review(self.reviewers[i], referral, approve=True,
                                                           note="Checked at the same time"))
        self.assertEqual(sum(1 for r in results if isinstance(r, Referral)), 1)
        self.assertEqual(ReferralReward.objects.count(), 1)
        self.assertEqual(JournalEntry.objects.filter(posting_type="referral.reward_pending").count(), 1)

    def test_daily_limit_holds_under_concurrency(self):
        referrals = [self.under_review(i) for i in range(2)]
        run_threads(2, lambda i: services.review(self.reviewers[i], referrals[i], approve=True, note="Parallel"))
        self.assertEqual(ReferralReward.objects.count(), 1)
        statuses = sorted(Referral.objects.values_list("status", "outcome_reason"))
        self.assertEqual(statuses, [("QUALIFIED", "daily_limit"), ("REWARD_PENDING", "")])
        self.assertEqual(funding.budget_summary()["programme_balance"], 4000)
        self.assertEqual(services.check_consistency(), [])
        self.assertTrue(User.objects.filter(pk=self.referrer.pk).exists())
        self.clock.advance(timedelta(days=1).total_seconds())
