"""REF-01: referral codes, attribution, qualification, funded rewards, clearance, FIN-02 withdrawal,
fraud review, reversals, policy versions, limits, activation switches and database guards."""

import re
from datetime import timedelta

from django.db import DatabaseError, IntegrityError, transaction
from django.test import override_settings

from apps.earnings import services as earnings
from apps.notifications.models import Notification
from apps.referrals import funding, services
from apps.referrals.models import MarketingDeposit, Referral, ReferralCode, ReferralPolicy, ReferralReward
from apps.viewing import services as viewing
from apps.wallets import services as ledger
from apps.wallets import wallet_services
from apps.wallets.chart import ACCOUNT_TYPES, PROTECTED_INFLOWS
from apps.wallets.models import JournalEntry, Wallet, WalletTransaction
from apps.wallets.posting_types import POSTING_TYPES
from apps.withdrawals import real
from apps.withdrawals.models import WithdrawalRequest

from apps.accounts.tests.helpers import make_user

from .helpers import REFERRALS_OFF, ReferralTestCase, User, reference, staff

R = Referral.Status
RW = ReferralReward.Status


def all_clean(testcase):
    testcase.assertEqual(earnings.check_consistency(), [])
    testcase.assertEqual(services.check_consistency(), [])
    testcase.assertEqual(real.reconcile(), [])
    testcase.assertEqual(wallet_services.reconcile()["findings"], 0)


class CodeTests(ReferralTestCase):
    def test_codes_are_unique_random_and_stable(self):
        self.assertRegex(self.code.code, r"^[A-HJ-NP-Z2-9]{8}$")
        self.assertEqual(services.ensure_code(self.member), self.code)
        codes = {services.ensure_code(make_user(f"c{i}@x.example")).code for i in range(25)}
        self.assertEqual(len(codes), 25)

    def test_staff_get_no_code_and_codes_are_immutable(self):
        self.assertIsNone(services.ensure_code(self.reviewer))
        with self.assertRaises(DatabaseError), transaction.atomic():
            ReferralCode.objects.filter(pk=self.code.pk).update(code="ABCDEFGH")
        with self.assertRaises(DatabaseError), transaction.atomic():
            ReferralCode.objects.filter(pk=self.code.pk).delete()

    def test_manipulated_codes_are_refused(self):
        for raw in ("", "abc", "ABCDEFG0", "ABCDEFGHI", "' OR 1=1 --", self.code.code[:-1] + "1"):
            self.assertIsNone(services.referrer_for_code(raw))
        self.assertEqual(services.referrer_for_code(f" {self.code.code.lower()[:4]}-{self.code.code[4:]} "), self.code)


class AttributionTests(ReferralTestCase):
    def test_registration_records_exactly_one_immutable_referral(self):
        user = self.join()
        referral = user.referral
        self.assertEqual((referral.referrer, referral.status, referral.source, referral.policy.version),
                         (self.member, R.AWAITING, "LINK", 1))
        self.assertTrue(Notification.objects.filter(recipient=self.member, kind="referrals.registered").exists())
        other = self.new_member("other@x.example")
        for change in ({"referrer": other}, {"code": "ABCDEFGH"}, {"source": "CODE"}):
            with self.assertRaises(DatabaseError), transaction.atomic():
                Referral.objects.filter(pk=referral.pk).update(**change)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Referral.objects.filter(pk=referral.pk).delete()
        with self.assertRaises(IntegrityError), transaction.atomic():  # One referrer per referred member.
            Referral.objects.create(referrer=other, referred=user, code=services.ensure_code(other).code,
                                    source="CODE", policy=services.current_policy())

    def test_no_retroactive_self_or_circular_referrals(self):
        existing = self.new_member("existing@x.example")
        User.objects.filter(pk=existing.pk).update(date_joined=self.clock.now - timedelta(hours=2))
        with self.assertRaises(IntegrityError), transaction.atomic():
            Referral.objects.create(referrer=self.member, referred=existing, code=self.code.code, source="CODE",
                                    policy=services.current_policy())
        with self.assertRaises(IntegrityError), transaction.atomic():
            Referral.objects.create(referrer=self.member, referred=self.member, code=self.code.code, source="CODE",
                                    policy=services.current_policy())
        user = self.join()
        with self.assertRaises(IntegrityError), transaction.atomic():  # B can never refer A back.
            Referral.objects.create(referrer=user, referred=self.member, code=services.ensure_code(user).code,
                                    source="CODE", policy=services.current_policy())

    def test_invalid_code_or_paused_programme_records_nothing_and_never_blocks_registration(self):
        self.assertFalse(hasattr(self.join(code="ZZZZZZZZ"), "referral_id"))
        self.assertEqual(Referral.objects.count(), 0)
        self.rules(tracking_open=False)
        user = self.join()
        self.assertFalse(Referral.objects.filter(referred=user).exists())

    def test_registration_without_a_code(self):
        user = self.join(code="")
        self.assertFalse(Referral.objects.filter(referred=user).exists())


class QualificationAndMoneyTests(ReferralTestCase):
    def setUp(self):
        super().setUp()
        self.rules()
        self.fund(10_000)

    def test_full_journey_pending_cleared_withdrawn_and_reconciled(self):
        user = self.join()
        referral = self.referral_for(self.qualify(user).referred)
        self.assertEqual(referral.status, R.REWARD_PENDING)
        reward = referral.reward
        self.assertEqual((reward.amount, reward.member, reward.status, reward.policy_version), (1000, self.member,
                                                                                               RW.PENDING, 2))
        self.assertEqual(reward.pending_entry.posting_type, "referral.reward_pending")
        wallet = Wallet.objects.get(member=self.member)
        self.assertEqual(wallet_services.balances(wallet).pending, 1000)
        self.assertEqual(WalletTransaction.objects.get(journal_entry=reward.pending_entry).category, "REFERRAL_CREDIT")
        self.assertEqual(funding.budget_summary()["programme_balance"], 9000)
        all_clean(self)
        # Clearance (the referral's 24 h period has already passed in qualify(): clock advanced).
        services.clear_due()
        reward.refresh_from_db()
        self.assertEqual((reward.status, self.referral_for(user).status), (RW.AVAILABLE, R.REWARD_CLEARED))
        self.assertEqual(wallet_services.balances(wallet).available, 1000)
        self.assertTrue(Notification.objects.filter(recipient=self.member, kind="referrals.available").exists())
        all_clean(self)
        # FIN-02: the cleared referral reward is withdrawn like any available balance.
        self.beneficiary = self.destination(member=self.member)
        withdrawal = self.submit(amount=600)
        self.pay(withdrawal)
        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.status, WithdrawalRequest.Status.PAID)
        self.assertEqual(wallet_services.balances(wallet).available, 400)
        all_clean(self)

    def test_registration_and_membership_alone_never_earn(self):
        user = self.join(membership=True)
        services.evaluate(user.referral.pk)
        self.assertEqual(self.referral_for(user).status, R.AWAITING)
        self.clock.advance(31 * 24 * 3600)
        services.evaluate_due()
        referral = self.referral_for(user)
        self.assertEqual((referral.status, referral.outcome_reason), (R.EXPIRED, "not_qualified_in_time"))
        self.assertFalse(JournalEntry.objects.filter(source_type="referral_reward").exists())

    def test_one_day_is_not_enough_and_revoked_views_do_not_count(self):
        user = self.join(membership=True)
        self.clock.advance(25 * 3600)  # Old enough: only the activity rule decides here.
        session = self.verified_view(member=user, campaign=self.qcampaign)
        self.verified_view(member=user, campaign=self.running_campaign(name="Same day"))
        self.assertEqual(self.referral_for(user).status, R.AWAITING)  # Two views, but on one day.
        user2 = self.join(membership=True)
        self.clock.advance(25 * 3600)
        first = self.verified_view(member=user2, campaign=self.qcampaign)
        viewing.revoke_verified_session(self.rewards_admin, first, reason="Fraud found later")
        self.clock.advance(25 * 3600)
        self.verified_view(member=user2, campaign=self.qcampaign)
        self.assertEqual(self.referral_for(user2).status, R.AWAITING)  # The revoked view does not count.
        self.assertTrue(session.pk)

    def test_activity_revoked_after_reward_reverses_it(self):
        user = self.join()
        self.qualify(user)
        session = user.watch_sessions.order_by("finalized_at").first()
        with self.captureOnCommitCallbacks(execute=True):
            viewing.revoke_verified_session(self.rewards_admin, session, reason="Fraud found later")
        referral = self.referral_for(user)
        self.assertEqual((referral.status, referral.outcome_reason), (R.REVERSED, "activity_revoked"))
        self.assertEqual(referral.reward.reversal_entry.posting_type, "referral.pending_reversed")
        self.assertEqual(funding.budget_summary()["programme_balance"], 10_000)
        all_clean(self)

    def test_insufficient_budget_then_retry(self):
        funding.release_budget(self.mk_recorder, amount=9_500, reason="Budget moved")
        user = self.join()
        referral = self.referral_for(self.qualify(user).referred)
        self.assertEqual((referral.status, referral.outcome_reason), (R.QUALIFIED, "funding_unavailable"))
        self.assertFalse(ReferralReward.objects.exists())
        funding.commit_budget(self.mk_recorder, amount=1_000)
        self.assertEqual(services.retry_allocations(), 1)
        self.assertEqual(self.referral_for(user).status, R.REWARD_PENDING)
        all_clean(self)

    def test_daily_and_lifetime_limits(self):
        self.rules(daily_reward_limit=1, lifetime_reward_limit=2)
        users = [self.join() for _ in range(3)]
        for user in users[:2]:
            self.active_membership(member=user)
        self.verified_view(member=users[0], campaign=self.qcampaign)
        self.verified_view(member=users[1], campaign=self.qcampaign)
        self.clock.advance(25 * 3600)
        self.verified_view(member=users[0], campaign=self.qcampaign)
        self.verified_view(member=users[1], campaign=self.qcampaign)
        statuses = sorted((self.referral_for(u).status, self.referral_for(u).outcome_reason) for u in users[:2])
        self.assertEqual(statuses, [(R.QUALIFIED, "daily_limit"), (R.REWARD_PENDING, "")])
        self.clock.advance(24 * 3600)
        self.assertEqual(services.retry_allocations(), 1)  # Next day.
        self.qualify(users[2])
        third = self.referral_for(users[2])
        self.assertEqual((third.status, third.outcome_reason), (R.QUALIFIED, "lifetime_limit"))
        self.assertEqual(ReferralReward.objects.count(), 2)

    def test_referrer_membership_condition(self):
        referrer = make_user("no-plan@x.example")  # Can refer (tracking), but has no membership.
        user = self.join(code=services.ensure_code(referrer).code)
        referral = self.referral_for(self.qualify(user).referred)
        self.assertEqual((referral.status, referral.outcome_reason), (R.QUALIFIED, "referrer_membership"))

    def test_qualification_is_idempotent(self):
        user = self.join()
        self.qualify(user)
        referral = self.referral_for(user)
        for _ in range(3):
            services.evaluate(referral.pk)
            services.retry_allocations()
            with transaction.atomic():
                locked = Referral.objects.select_for_update().get(pk=referral.pk)
                if locked.status in (R.AWAITING, R.UNDER_REVIEW, R.QUALIFIED):
                    services._allocate(locked, self.clock.now)
        self.assertEqual(ReferralReward.objects.filter(referral=referral).count(), 1)
        self.assertEqual(JournalEntry.objects.filter(posting_type="referral.reward_pending").count(), 1)

    def test_restricted_wallet_does_not_clear_and_reversal_after_withdrawal_needs_recovery(self):
        user = self.join()
        self.qualify(user)
        wallet = Wallet.objects.get(member=self.member)
        wallet_services.place_restriction(self.root, wallet, kind="FRAUD_REVIEW", reason="Referral review")
        services.clear_due()
        self.assertEqual(self.referral_for(user).reward.status, RW.PENDING)
        restriction = wallet.restrictions.get()
        wallet_services.request_lift(self.root, restriction, reason="Review complete")
        wallet_services.decide_lift(staff("lift@x.example", "wallets.manage_wallet_restrictions"), restriction,
                                    approve=True)
        services.clear_due()
        reward = self.referral_for(user).reward
        self.assertEqual(reward.status, RW.AVAILABLE)
        self.beneficiary = self.destination(member=self.member)
        self.pay(self.submit(amount=1000))
        services.staff_reverse(self.reviewer, reward, reason="Found to break the rules")
        reward.refresh_from_db()
        self.assertEqual(reward.status, RW.RECOVERY_REQUIRED)
        self.assertEqual(Wallet.objects.get(pk=wallet.pk).status, Wallet.Status.RESTRICTED)
        self.assertGreaterEqual(wallet_services.balances(wallet).available, 0)
        all_clean(self)


class ReviewTests(ReferralTestCase):
    def setUp(self):
        super().setUp()
        self.rules()
        self.fund(5_000)

    def flagged(self):
        user = self.join()
        self.destination(member=self.member, number="0754999888", age_hours=0)
        self.destination(member=user, number="0754999888", age_hours=0)
        referral = self.referral_for(self.qualify(user).referred)
        self.assertEqual(referral.status, R.UNDER_REVIEW)
        self.assertIn("shared_payout_destination", referral.risk_flags)
        self.assertFalse(ReferralReward.objects.exists())
        return referral

    def test_risk_signals_send_to_review_then_approval_uses_the_normal_rules(self):
        referral = self.flagged()
        self.assertTrue(Notification.objects.filter(recipient=self.reviewer, kind="referrals.review_needed").exists())
        with self.assertRaises(services.NotAuthorized):
            services.review(self.mk_verifier, referral, approve=True, note="Not a reviewer")
        services.review(self.reviewer, referral, approve=True, note="Family members, checked")
        referral.refresh_from_db()
        self.assertEqual((referral.status, referral.reward.amount), (R.REWARD_PENDING, 1000))
        with self.assertRaises(services.ReferralError):  # Decided: a second approval changes nothing.
            services.review(self.reviewer, referral, approve=True, note="Again")
        self.assertEqual(ReferralReward.objects.count(), 1)

    def test_rejection(self):
        referral = self.flagged()
        services.review(self.reviewer, referral, approve=False, note="Same person, two accounts")
        referral.refresh_from_db()
        self.assertEqual((referral.status, referral.outcome_reason), (R.REJECTED, "review_rejected"))
        self.assertTrue(Notification.objects.filter(recipient=self.member, kind="referrals.rejected").exists())
        with self.assertRaises(IntegrityError), transaction.atomic():  # Final.
            Referral.objects.filter(pk=referral.pk).update(status=R.REWARD_PENDING)

    def test_parties_cannot_review_and_staff_cannot_grant_arbitrary_amounts(self):
        referral = self.flagged()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Referral.objects.filter(pk=referral.pk).update(reviewed_by=self.member)
        wallet = wallet_services.ensure_wallet(self.member)
        entry = ledger.post_entry("referral.reward_pending",
                                  lines=[ledger.debit(services.programme_account(), 5000, "x"),
                                         ledger.credit(wallet.pending_account, 5000, "x")],
                                  idempotency_key="test-arbitrary", source_type="test", source_id="x",
                                  description="Arbitrary grant attempt")
        with self.assertRaises(IntegrityError), transaction.atomic():  # Above the policy amount.
            ReferralReward.objects.create(referral=referral, member=self.member, policy_version=2, amount=5000,
                                          reward_day=self.clock.now.date(), clears_at=self.clock.now,
                                          pending_entry=entry)

    def test_staff_reverse_pending(self):
        referral = self.flagged()
        services.review(self.reviewer, referral, approve=True, note="Checked")
        reward = ReferralReward.objects.get()
        services.set_hold(self.reviewer, reward, hold=True, reason="Second look")
        self.clock.advance(48 * 3600)
        services.clear_due()
        reward.refresh_from_db()
        self.assertEqual(reward.status, RW.PENDING)  # On hold: never clears.
        services.staff_reverse(self.reviewer, reward, reason="Confirmed abuse")
        reward.refresh_from_db()
        self.assertEqual(reward.status, RW.REVERSED)
        self.assertEqual(funding.budget_summary()["programme_balance"], 5_000)
        all_clean(self)


class PolicyTests(ReferralTestCase):
    def test_four_eyes_and_final_versions(self):
        current = services.current_policy()
        values = {**{name: getattr(current, name) for name in services.POLICY_FIELDS}, "rewards_open": True,
                  "reward_amount": 500, "daily_reward_limit": "", "lifetime_reward_limit": ""}
        proposal = services.propose_policy(self.proposer, expected_version=1, reason="Start rewards", **values)
        self.assertEqual(services.current_policy().version, 1)  # Not in force until approved.
        both = staff("both@x.example", "referrals.manage_referral_policy", "referrals.approve_referral_policy")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReferralPolicy.objects.filter(pk=proposal.pk).update(status="ACTIVE", decided_by=self.proposer,
                                                                 decided_at=self.clock.now)
        services.decide_policy(self.policy_approver, proposal, approve=True)
        self.assertEqual(services.current_policy().version, 2)
        with self.assertRaises(DatabaseError), transaction.atomic():
            ReferralPolicy.objects.filter(pk=proposal.pk).update(reward_amount=99_999)
        with self.assertRaises(IntegrityError), transaction.atomic():  # Never inserted as approved.
            ReferralPolicy.objects.create(version=9, status="ACTIVE", rewards_open=True, reward_amount=1,
                                          decided_by=self.policy_approver, decided_at=self.clock.now,
                                          created_by=self.proposer)
        second = services.propose_policy(both, expected_version=2, reason="Try self approval",
                                         **{**values, "reward_amount": 600})
        with self.assertRaises(services.NotAuthorized):
            services.decide_policy(both, second, approve=True)

    def test_rule_changes_never_raise_an_existing_referral_and_can_lower_or_stop_it(self):
        self.rules()
        self.fund(10_000)
        early = self.join()
        self.rules(reward_amount=3000)  # Raised for new referrals only.
        late = self.join()
        self.qualify(early)
        self.qualify(late)
        self.assertEqual(self.referral_for(early).reward.amount, 1000)
        self.assertEqual(self.referral_for(late).reward.amount, 3000)
        lowered = self.join()
        self.rules(reward_amount=400)
        self.qualify(lowered)
        self.assertEqual(self.referral_for(lowered).reward.amount, 400)
        stopped = self.join()
        self.rules(rewards_open=False, reward_amount=0)
        self.qualify(stopped)
        referral = self.referral_for(stopped)
        self.assertEqual((referral.status, referral.outcome_reason), (R.QUALIFIED, "programme_closed"))
        self.assertEqual(self.referral_for(early).reward.amount, 1000)  # Allocated rewards never change.

    def test_referral_registered_before_rewards_never_earns_retroactively(self):
        self.rules(rewards_open=False, reward_amount=0)  # Programme open, no reward offered yet.
        user = self.join()
        self.rules(rewards_open=True, reward_amount=1000)
        self.fund(5_000)
        referral = self.referral_for(self.qualify(user).referred)
        self.assertEqual((referral.status, referral.outcome_reason), (R.QUALIFIED, "no_reward_offered"))


class ActivationTests(ReferralTestCase):
    @REFERRALS_OFF
    def test_tracking_works_without_money(self):
        self.rules()
        user = self.join()
        referral = self.referral_for(self.qualify(user).referred)
        self.assertEqual((referral.status, referral.outcome_reason), (R.QUALIFIED, "rewards_inactive"))
        self.assertFalse(JournalEntry.objects.filter(posting_type__startswith="referral.").exists())
        deposit = funding.record_deposit(self.mk_recorder, method="BANK_TRANSFER", amount=1000,
                                         external_reference=reference(), paid_on=self.clock.now.date())
        with self.assertRaises(Exception):
            funding.verify_deposit(self.mk_verifier, deposit, receipt_confirmed=True)

    def test_switching_off_new_rewards_keeps_settlement(self):
        self.rules()
        self.fund(5_000)
        user = self.join()
        self.qualify(user)
        with override_settings(REFERRAL_REWARDS_ENABLED=False):
            self.assertFalse(services.rewards_active())
            self.assertTrue(services.settlement_active())
            services.clear_due()
            self.assertEqual(self.referral_for(user).reward.status, RW.AVAILABLE)

    @override_settings(RESTRICTED_DEMO_MODE=True)
    def test_demo_mode_never_activates_money(self):
        self.assertFalse(services.rewards_active())
        self.assertFalse(services.settlement_active())
        from apps.core.checks import check_financial_locks

        self.assertIn("core.E015", [error.id for error in check_financial_locks(None)])

    def test_financial_switches_off_by_default(self):
        with override_settings(REFERRAL_REWARDS_ENABLED=False, REFERRAL_REWARDS_APPROVED=False):
            self.assertFalse(services.rewards_active() or services.settlement_active())


class FundingTests(ReferralTestCase):
    def test_marketing_deposit_four_eyes_and_reference_reuse(self):
        ref = reference()
        deposit = funding.record_deposit(self.mk_recorder, method="AIRTEL_LIPA", amount=2000, external_reference=ref,
                                         paid_on=self.clock.now.date())
        both = staff("both-mk@x.example", "referrals.record_marketing_deposits", "referrals.verify_marketing_deposits")
        own = funding.record_deposit(both, method="AIRTEL_LIPA", amount=500, external_reference=reference(),
                                     paid_on=self.clock.now.date())
        with self.assertRaises(services.NotAuthorized):
            funding.verify_deposit(both, own, receipt_confirmed=True)
        with self.assertRaises(Exception):  # The same Airtel transaction cannot back two deposits.
            funding.record_deposit(self.mk_recorder, method="AIRTEL_LIPA", amount=2000, external_reference=ref,
                                   paid_on=self.clock.now.date())
        with self.assertRaises(Exception):  # ... nor a campaign deposit.
            self.deposit(ref=ref)
        funding.verify_deposit(self.mk_verifier, deposit, receipt_confirmed=True)
        self.assertEqual(funding.budget_summary()["unallocated"], 2000)
        with self.assertRaises(Exception):
            funding.commit_budget(self.mk_recorder, amount=2001)
        funding.commit_budget(self.mk_recorder, amount=2000)
        self.assertEqual(funding.budget_summary()["programme_balance"], 2000)
        with self.assertRaises(DatabaseError), transaction.atomic():
            MarketingDeposit.objects.filter(pk=deposit.pk).update(amount=9_999_999)
        all_clean(self)

    def test_only_marketing_money_funds_referrals(self):
        programme = ACCOUNT_TYPES["REFERRAL_PROGRAMME_FUNDING"]
        allowed = PROTECTED_INFLOWS[programme.funding_class]
        self.assertNotIn("MEMBERSHIP", allowed)
        sources = {t for p in POSTING_TYPES.values() if "REFERRAL_PROGRAMME_FUNDING" in p.credit_types
                   for t in p.debit_types}
        self.assertEqual(sources, {"MARKETING_FUNDING_VERIFIED", "MEMBER_REWARD_PENDING", "REWARD_LIABILITY"})
        self.assertNotIn("CAMPAIGN_RESTRICTED_FUNDING", sources)
        self.assertFalse(any("CAMPAIGN_RESTRICTED_FUNDING" in p.debit_types and "MEMBER_REWARD_PENDING" in
                             p.credit_types for code, p in POSTING_TYPES.items() if code.startswith("referral.")))
