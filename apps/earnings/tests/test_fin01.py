"""FIN-01: real TZS rewards and campaign funding, end to end on the real ledger (test database only)."""

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import translation

from apps.advertising import services as advertising
from apps.advertising.models import Campaign
from apps.audit.models import AuditEvent
from apps.core import features
from apps.earnings import funding, rules, services
from apps.earnings.models import CampaignDeposit, PlanEarningLimit, SessionReward
from apps.notifications import inbox
from apps.notifications.models import Notification
from apps.rewards import services as rewards
from apps.rewards.models import DemoReward
from apps.viewing import services as viewing
from apps.viewing.models import WatchSession
from apps.wallets import services as ledger
from apps.wallets import wallet_services
from apps.wallets.exceptions import InsufficientFundsError, LedgerError, PostingValidationError
from apps.wallets.models import JournalEntry, Wallet, WalletTransaction

from .helpers import FinanceTestCase, reference, staff

RS = SessionReward.Status


def balances(member):
    return wallet_services.balances(Wallet.objects.get(member=member))


class ActivationTests(FinanceTestCase):
    @override_settings(REAL_MONEY_REWARDS_ENABLED=False)
    def test_off_by_default_no_offer_and_nothing_posted(self):
        self.assertFalse(features.is_enabled("real_money_rewards"))
        policy = rewards.create_policy(self.root, self.campaign, reward_amount=100, kind="TZS")
        rewards.approve_policy(self.approver, policy)  # Rates can be prepared while switched off.
        self.assertEqual(rewards.current_offer(self.campaign), (None, 0))  # A TZS policy offers no credits.
        session = self.start()
        self.assertFalse(SessionReward.objects.exists())
        self.assertEqual(services.preview_offer(self.member, self.campaign).amount, 0)
        deposit = self.deposit(verify=False)
        with self.assertRaises(ValidationError):
            funding.verify_deposit(self.verifier, deposit, receipt_confirmed=True)
        self.assertFalse(JournalEntry.objects.exists())
        self.play(session)
        self.assertFalse(DemoReward.objects.filter(session=session, status=DemoReward.Status.QUALIFIED).exists())

    def test_approval_setting_and_demo_mode_are_both_required(self):
        self.assertTrue(features.is_enabled("real_money_rewards"))
        with override_settings(REAL_MONEY_REWARDS_APPROVED=False):
            self.assertFalse(features.is_enabled("real_money_rewards"))
        with override_settings(RESTRICTED_DEMO_MODE=True):
            self.assertFalse(features.is_enabled("real_money_rewards"))
        self.assertFalse(features.is_enabled("withdrawals"))
        self.assertFalse(features.is_enabled("live_payments"))

    def test_deploy_checks(self):
        from apps.core.checks import check_financial_locks

        ids = {message.id for message in check_financial_locks(None)}
        self.assertIn("core.W011", ids)
        self.assertNotIn("core.E008", ids)
        with override_settings(RESTRICTED_DEMO_MODE=True):
            self.assertIn("core.E009", {message.id for message in check_financial_locks(None)})
        with override_settings(PAYMENTS_MODE="sandbox"):
            self.assertIn("core.E010", {message.id for message in check_financial_locks(None)})


class DepositTests(FinanceTestCase):
    def test_recording_moves_no_money_and_needs_a_second_person(self):
        deposit = self.deposit(verify=False)
        self.assertEqual(deposit.status, CampaignDeposit.Status.RECORDED)
        self.assertFalse(JournalEntry.objects.exists())  # A typed reference is never proof of payment.
        self.assertEqual(funding.organization_balance(self.org), 0)
        both = staff("both@x.example", "earnings.record_deposits", "earnings.verify_deposits")
        own = funding.record_deposit(both, organization=self.org, method="AIRTEL_LIPA", amount=5_000,
                                     external_reference=reference(), paid_on=deposit.paid_on)
        with self.assertRaises(services.NotAuthorized):
            funding.verify_deposit(both, own, receipt_confirmed=True)
        with self.assertRaises(ValidationError):
            funding.verify_deposit(self.verifier, deposit, receipt_confirmed=False)
        verified = funding.verify_deposit(self.verifier, deposit, receipt_confirmed=True)
        self.assertEqual(verified.status, CampaignDeposit.Status.VERIFIED)
        self.assertEqual(funding.organization_balance(self.org), 10_000)
        self.assertEqual(set(JournalEntry.objects.values_list("posting_type", flat=True)),
                         {"advertiser.manual_deposit_received", "advertiser.manual_deposit_verified"})
        verified_entry = JournalEntry.objects.get(posting_type="advertiser.manual_deposit_verified")
        self.assertEqual(verified_entry.related_entry.posting_type, "advertiser.manual_deposit_received")
        again = funding.verify_deposit(self.verifier, deposit, receipt_confirmed=True,
                                       expected_status=CampaignDeposit.Status.VERIFIED)
        self.assertEqual(again.pk, verified.pk)
        self.assertEqual(JournalEntry.objects.count(), 2)  # Replays post nothing.

    def test_duplicate_references_per_channel(self):
        ref = reference()
        self.deposit(ref=ref)
        with self.assertRaises(ValidationError):
            self.deposit(ref=ref.lower(), verify=False)  # Same Airtel transaction, any spelling.
        self.deposit(ref=ref, method="BANK_TRANSFER")  # Another channel is another transaction.
        rejected = self.deposit(ref="BANK-998877", method="BANK_TRANSFER", verify=False)
        funding.reject_deposit(self.verifier, rejected, reason="Not in the bank statement")
        self.deposit(ref="BANK-998877", method="BANK_TRANSFER", verify=False)  # Rejected references can be reused.

    def test_bank_and_clipcash_organization_deposits(self):
        clipcash = self.make_org(self.owner, "ClipCash Promotions")
        clipcash = advertising.review_organization(self.root, clipcash, "approve", expected_status="PENDING")
        self.deposit(org=clipcash, amount=50_000, method="BANK_TRANSFER", ref="CRDB-2026-0001")
        self.assertEqual(funding.organization_balance(clipcash), 50_000)
        self.assertEqual(funding.organization_balance(self.org), 0)

    def test_database_guards(self):
        deposit = self.deposit(verify=False)
        with self.assertRaises((IntegrityError, DatabaseError)), transaction.atomic():
            CampaignDeposit.objects.filter(pk=deposit.pk).update(amount=999_999)
        with self.assertRaises((IntegrityError, DatabaseError)), transaction.atomic():
            CampaignDeposit.objects.filter(pk=deposit.pk).delete()
        with self.assertRaises((IntegrityError, DatabaseError)), transaction.atomic():
            CampaignDeposit.objects.filter(pk=deposit.pk).update(decided_by=self.recorder, status="REJECTED",
                                                                decision_note="x", reference_lock=None)


class FundingTests(FinanceTestCase):
    def test_commit_and_release(self):
        self.deposit(amount=5_000)
        with self.assertRaises(ValidationError):
            funding.commit_funding(self.recorder, self.campaign, amount=6_000)
        funding.commit_funding(self.recorder, self.campaign, amount=3_000)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.funding_status, Campaign.Funding.VERIFIED)
        self.assertEqual(self.campaign.funding_entry.posting_type, "campaign.funding_committed")
        summary = funding.campaign_summary(self.campaign)
        self.assertEqual((summary["committed"], summary["remaining"]), (3_000, 3_000))
        self.assertEqual(funding.organization_balance(self.org), 2_000)
        funding.release_funding(self.recorder, self.campaign, amount=1_000, reason="Campaign shortened")
        self.assertEqual(funding.campaign_summary(self.campaign)["remaining"], 2_000)
        self.assertEqual(funding.organization_balance(self.org), 3_000)

    def test_release_never_touches_reserved_funds(self):
        self.fund(amount=100)
        self.tzs_policy(amount=100)
        self.start()  # Reserves the whole 100.
        with self.assertRaises(ValidationError):
            funding.release_funding(self.recorder, self.campaign, amount=1, reason="Try to take reserved")

    def test_campaign_cannot_be_marked_funded_without_evidence(self):
        with self.assertRaises((IntegrityError, DatabaseError)), transaction.atomic():
            Campaign.objects.filter(pk=self.campaign.pk).update(funding_status="VERIFIED")
        other = self.running_campaign(name="Other")
        entry = self.fund(campaign=other, amount=500)
        with self.assertRaises((IntegrityError, DatabaseError)), transaction.atomic():
            Campaign.objects.filter(pk=self.campaign.pk).update(funding_status="VERIFIED", funding_entry=entry)

    def test_membership_money_can_never_fund_rewards(self):
        cash = ledger.get_platform_account("CASH")
        target = services.funding_account(self.campaign.pk, create=True)
        membership = ledger.get_platform_account("MEMBERSHIP_DEFERRED_REVENUE")
        for source in (cash, membership):
            with self.assertRaises(PostingValidationError):
                ledger.post_entry("campaign.funding_committed", lines=[ledger.debit(source, 100),
                                                                     ledger.credit(target, 100)],
                                  idempotency_key=f"bad:{source.pk}", source_type="test", description="bad")


class RewardFlowTests(FinanceTestCase):
    def test_tzs_100_reward_from_offer_to_available(self):
        self.fund(amount=1_000)
        self.tzs_policy(amount=100)
        offer = services.preview_offer(self.member, self.campaign)
        self.assertEqual((offer.amount, offer.reason), (100, ""))
        session = self.start()
        reward = session.earning
        self.assertEqual((reward.status, reward.amount), (RS.RESERVED, 100))
        self.assertEqual(funding.campaign_summary(self.campaign)["remaining"], 900)
        self.play(session)  # Completion settles automatically (on commit).
        reward.refresh_from_db()
        self.assertEqual(reward.status, RS.PENDING)
        self.assertEqual((balances(self.member).pending, balances(self.member).available), (100, 0))
        self.assertEqual(reward.clears_at - reward.decided_at, timedelta(hours=24))
        self.assertFalse(DemoReward.objects.filter(session=session).exists())  # Never credits too.
        self.assertIsNone(rewards.evaluate_session(session.pk))
        self.assertEqual(services.clear_due()["cleared"], 0)  # Not due yet.
        self.clock.advance(24 * 3600 + 1)
        self.assertEqual(services.clear_due()["cleared"], 1)
        self.assertEqual(services.clear_due()["cleared"], 0)  # Idempotent.
        reward.refresh_from_db()
        self.assertEqual(reward.status, RS.AVAILABLE)
        self.assertEqual((balances(self.member).pending, balances(self.member).available), (0, 100))
        txns = WalletTransaction.objects.filter(wallet__member=self.member)  # Clearance rows share a timestamp.
        self.assertEqual(sorted((t.bucket, t.direction, t.amount) for t in txns),
                         [("AVAILABLE", "IN", 100), ("PENDING", "IN", 100), ("PENDING", "OUT", 100)])
        summary = funding.campaign_summary(self.campaign)
        self.assertEqual((summary["consumed"], summary["remaining"], summary["available"]), (100, 900, 100))
        self.assertEqual(services.check_consistency(), [])
        kinds = set(Notification.objects.filter(recipient=self.member).values_list("kind", flat=True))
        self.assertTrue({"earnings.pending", "earnings.available"} <= kinds)

    def test_50_and_100_campaigns_no_reward_and_credits_campaigns(self):
        fifty = self.running_campaign(name="Fifty")
        none = self.running_campaign(name="No reward")
        credits = self.running_campaign(name="Credits")
        self.fund(amount=1_000)
        self.fund(campaign=fifty, amount=500)
        self.tzs_policy(amount=100)
        self.tzs_policy(campaign=fifty, amount=50)
        rewards.approve_policy(self.approver, rewards.create_policy(self.root, credits, reward_amount=20))
        with override_settings(VIEWING_MAX_CONCURRENT_SESSIONS=1):
            for campaign in (self.campaign, fifty, none, credits):
                self.verified_view(campaign=campaign)
        amounts = dict(SessionReward.objects.values_list("campaign__name", "amount"))
        self.assertEqual(amounts, {"Launch": 100, "Fifty": 50})
        self.assertEqual(balances(self.member).pending, 150)
        rewards.evaluate_pending()
        credit = DemoReward.objects.get(campaign=credits)
        self.assertEqual((credit.status, credit.amount), (DemoReward.Status.QUALIFIED, 20))
        self.assertEqual(balances(self.member).pending, 150)  # Credits never reach the wallet.

    def test_ineligible_plan_gets_no_offer(self):
        from apps.rewards.models import CampaignCreditTerms

        gold = self.make_plan("gold", price=9_000)
        CampaignCreditTerms.objects.create(campaign=self.campaign, credit_budget=100).eligible_plans.add(gold)
        self.fund(amount=1_000)
        self.tzs_policy(amount=100)
        with self.assertRaises(viewing.Denied) as caught:
            self.start()
        self.assertEqual(caught.exception.code, "plan_not_targeted")
        golden = self.new_member("gold@x.example", plan=gold)
        self.assertEqual(self.verified_view(member=golden).earning.status, RS.PENDING)

    def test_insufficient_funding_and_abandoned_session_release(self):
        self.fund(amount=100)
        self.tzs_policy(amount=100)
        other = self.new_member("other@x.example")
        first = self.start()
        self.assertEqual(services.preview_offer(other, self.campaign).reason, "reward_funding_unavailable")
        with self.assertRaises(viewing.Denied) as caught:
            self.start(member=other)
        self.assertEqual(caught.exception.code, "reward_funding_unavailable")
        self.clock.advance(400)  # The first member never plays: the session expires.
        with self.captureOnCommitCallbacks(execute=True):
            viewing.expire_sessions()
        first.earning.refresh_from_db()
        self.assertEqual((first.earning.status, first.earning.reasons), (RS.RELEASED, ["not_started"]))
        self.assertEqual(self.verified_view(member=other).earning.status, RS.PENDING)

    def test_early_exit_and_invalid_sessions_release(self):
        self.fund(amount=1_000)
        self.tzs_policy(amount=100)
        session = self.start()
        with self.captureOnCommitCallbacks(execute=True):
            self.play(session, until_s=30, end=False)
            viewing.finish_session(self.member, session.pk)
        reward = SessionReward.objects.get(session=session)
        self.assertEqual((reward.status, reward.reasons), (RS.RELEASED, ["watch_time_short"]))
        self.assertEqual(balances(self.member).pending, 0)
        self.assertEqual(funding.campaign_summary(self.campaign)["remaining"], 1_000)
        self.assertTrue(Notification.objects.filter(recipient=self.member, kind="earnings.not_rewarded").exists())

    def test_quoted_amount_survives_a_rate_change(self):
        self.fund(amount=1_000)
        self.tzs_policy(amount=100)
        session = self.start()
        self.tzs_policy(amount=50)  # Approved during playback.
        self.play(session)
        self.assertEqual(SessionReward.objects.get(session=session).amount, 100)
        self.assertEqual(services.preview_offer(self.member, self.campaign).amount, 50)

    def test_exactly_one_reward_per_session(self):
        self.fund(amount=1_000)
        self.tzs_policy(amount=100)
        session = self.verified_view()
        reward = session.earning
        for _ in range(3):
            services.settle(reward.pk)
            services.settle_due()
        viewing.finish_session(self.member, session.pk)
        self.assertEqual(JournalEntry.objects.filter(posting_type="reward.pending").count(), 1)
        self.assertEqual(balances(self.member).pending, 100)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SessionReward.objects.create(session=session, member=self.member, campaign=self.campaign,
                                         policy=reward.policy, policy_version=reward.policy_version, amount=100,
                                         earning_day=reward.earning_day, reserved_at=reward.reserved_at)

    def test_completed_campaign_keeps_rewards_and_reward_rows_are_guarded(self):
        self.fund(amount=1_000)
        self.tzs_policy(amount=100)
        reward = self.verified_view().earning
        with self.assertRaises((IntegrityError, DatabaseError)), transaction.atomic():
            SessionReward.objects.filter(pk=reward.pk).update(amount=1_000)
        with self.assertRaises((IntegrityError, DatabaseError)), transaction.atomic():
            SessionReward.objects.filter(pk=reward.pk).update(status="RESERVED")
        with self.assertRaises((IntegrityError, DatabaseError)), transaction.atomic():
            SessionReward.objects.filter(pk=reward.pk).delete()
        with self.assertRaises((IntegrityError, DatabaseError)), transaction.atomic():
            WalletTransaction.objects.filter(wallet__member=self.member).update(amount=1)


class LimitTests(FinanceTestCase):
    def setUp(self):
        super().setUp()
        self.second = self.running_campaign(name="Second")
        self.fund(amount=1_000)
        self.fund(campaign=self.second, amount=1_000)
        self.tzs_policy(amount=100)
        self.tzs_policy(campaign=self.second, amount=100)

    def settings(self, **values):
        row = services.earning_settings()
        current = {"daily_amount_limit": row.daily_amount_limit, "daily_view_limit": row.daily_view_limit,
                   "clearance_hours": row.clearance_hours}
        return rules.update_settings(self.rewards_admin, expected_version=row.version, **{**current, **values})

    def test_platform_view_limit(self):
        self.settings(daily_view_limit=1)
        self.verified_view()
        self.assertEqual(services.preview_offer(self.member, self.second).reason, "earning_view_limit")
        with self.assertRaises(viewing.Denied) as caught:
            self.start(campaign=self.second)
        self.assertEqual(caught.exception.code, "earning_view_limit")

    def test_plan_limit_is_stricter_and_never_above_the_platform(self):
        self.settings(daily_amount_limit=150)
        rules.set_plan_limit(self.rewards_admin, self.plan, daily_amount_limit=1_000, daily_view_limit=None)
        self.assertEqual(services.limits_for(self.plan.pk).amount, 150)  # The plan cannot raise it.
        rules.set_plan_limit(self.rewards_admin, self.plan, daily_amount_limit=None, daily_view_limit=1)
        self.assertEqual(services.limits_for(self.plan.pk).views, 1)
        self.verified_view()
        self.assertEqual(services.preview_offer(self.member, self.second).reason, "earning_view_limit")
        PlanEarningLimit.objects.filter(plan=self.plan).update(daily_view_limit=None)
        self.assertEqual(services.preview_offer(self.member, self.second).reason, "earning_amount_limit")

    def test_limits_reset_on_the_dar_es_salaam_day(self):
        self.assertEqual(services.local_day(datetime(2026, 11, 25, 21, 30, tzinfo=dt_timezone.utc)).isoformat(),
                         "2026-11-26")  # 00:30 EAT.
        self.assertEqual(services.local_day(datetime(2026, 11, 25, 20, 59, tzinfo=dt_timezone.utc)).isoformat(),
                         "2026-11-25")  # 23:59 EAT.
        self.settings(daily_view_limit=1)
        self.verified_view()
        start, end = services.day_bounds(services.local_day(self.clock.now))
        self.clock.now = end + timedelta(minutes=1)
        self.assertEqual(services.preview_offer(self.member, self.second).reason, "")

    def test_rules_are_audited_and_permissioned(self):
        with self.assertRaises(services.NotAuthorized):
            rules.update_settings(self.recorder, expected_version=1, daily_amount_limit=1, daily_view_limit=1,
                                  clearance_hours=24)
        self.settings(clearance_hours=48)
        self.assertTrue(AuditEvent.objects.filter(action="earnings.rules.updated").exists())
        reward = self.verified_view().earning
        self.assertEqual(reward.clears_at - reward.decided_at, timedelta(hours=48))


class FraudAndReversalTests(FinanceTestCase):
    def setUp(self):
        super().setUp()
        self.fund(amount=1_000)
        self.tzs_policy(amount=100)

    def test_review_holds_capacity_then_confirm_or_reject(self):
        session = self.start()
        with mock.patch("apps.viewing.services.fraud_flags", return_value=["large_seeks"]):
            self.play(session)
        reward = session.earning
        reward.refresh_from_db()
        self.assertEqual((session.outcome, reward.status), (WatchSession.Outcome.SUSPICIOUS_REVIEW, RS.RESERVED))
        with self.captureOnCommitCallbacks(execute=True):
            viewing.review_session(self.rewards_admin, session, "confirm", expected_status="PENDING",
                                   reason="Looked fine")
        reward.refresh_from_db()
        self.assertEqual(reward.status, RS.PENDING)

    def test_rejected_review_releases(self):
        session = self.start()
        with mock.patch("apps.viewing.services.fraud_flags", return_value=["large_seeks"]):
            self.play(session)
        with self.captureOnCommitCallbacks(execute=True):
            viewing.review_session(self.rewards_admin, session, "reject", expected_status="PENDING",
                                   reason="Automated playback")
        reward = SessionReward.objects.get(session=session)
        self.assertEqual(reward.status, RS.RELEASED)
        self.assertEqual(funding.campaign_summary(self.campaign)["remaining"], 1_000)

    def test_hold_blocks_clearance(self):
        reward = self.verified_view().earning
        services.set_hold(self.rewards_admin, reward, hold=True, reason="Checking a pattern")
        self.clock.advance(25 * 3600)
        self.assertEqual(services.clear_due()["cleared"], 0)
        services.set_hold(self.rewards_admin, reward, hold=False, reason="Pattern was fine")
        self.assertEqual(services.clear_due()["cleared"], 1)

    def test_revoking_a_view_reverses_a_pending_reward(self):
        session = self.verified_view()
        viewing.revoke_verified_session(self.rewards_admin, session, reason="Fraud ring")
        reward = SessionReward.objects.get(session=session)
        self.assertEqual(reward.status, RS.REVERSED)
        self.assertEqual(balances(self.member).pending, 0)
        self.assertEqual(funding.campaign_summary(self.campaign)["remaining"], 1_000)
        self.assertEqual(services.check_consistency(), [])

    def test_reversal_after_clearance(self):
        reward = self.verified_view().earning
        self.clock.advance(25 * 3600)
        services.clear_due()
        services.staff_reverse(self.rewards_admin, reward, reason="Chargeback by advertiser")
        reward.refresh_from_db()
        self.assertEqual(reward.status, RS.REVERSED)
        self.assertEqual((balances(self.member).available, balances(self.member).pending), (0, 0))
        self.assertEqual(services.staff_reverse(self.rewards_admin, reward, reason="Twice").status, RS.REVERSED)
        self.assertEqual(JournalEntry.objects.filter(posting_type="reward.cleared_reversed").count(), 1)

    def test_unrecoverable_reversal_restricts_instead_of_going_negative(self):
        reward = self.verified_view().earning
        self.clock.advance(25 * 3600)
        services.clear_due()
        with mock.patch.object(ledger, "post_entry", side_effect=InsufficientFundsError("short")):
            services.staff_reverse(self.rewards_admin, reward, reason="Funds already spent")
        reward.refresh_from_db()
        self.assertEqual(reward.status, RS.RECOVERY_REQUIRED)
        wallet = Wallet.objects.get(member=self.member)
        self.assertEqual(wallet.status, Wallet.Status.RESTRICTED)
        self.assertEqual(balances(self.member).available, 100)  # Nothing taken, nothing negative.
        self.assertEqual(services.clear_due()["cleared"], 0)

    def test_withdrawals_and_other_wallet_postings_stay_locked(self):
        reward = self.verified_view().earning
        wallet = Wallet.objects.get(member=self.member)
        with self.assertRaises((LedgerError, DatabaseError)), transaction.atomic():  # Refused by the database.
            ledger.post_entry("reward.allocated", lines=[
                ledger.debit(services.funding_account(self.campaign.pk), 10), ledger.credit(wallet.available_account, 10)],
                idempotency_key="sneaky", source_type="test", description="not a reward posting")
        from apps.withdrawals import services as withdrawals

        self.assertFalse(features.is_enabled("withdrawals"))
        self.assertNotEqual(withdrawals.withdrawal_eligibility(self.member).reasons, [])
        self.assertEqual(reward.status, RS.PENDING)


class ConsoleAndLanguageTests(FinanceTestCase):
    def test_console_pages_and_actions(self):
        self.client.force_login(self.root)
        for name in ("video_rewards", "campaign_funding", "earning_rules"):
            self.assertEqual(self.client.get(reverse(f"administration:{name}")).status_code, 200, name)
        response = self.client.post(reverse("administration:campaign_funding"), {
            "organization": self.org.pk, "method": "AIRTEL_LIPA", "amount": "2000",
            "external_reference": reference(), "paid_on": self.clock.now.date().isoformat(), "note": "Invoice 7"})
        deposit = CampaignDeposit.objects.get()
        self.assertRedirects(response, reverse("administration:campaign_deposit", args=[deposit.pk]))
        self.client.force_login(self.verifier)
        self.client.post(reverse("administration:campaign_deposit", args=[deposit.pk]), {
            "action": "verify", "receipt_confirmed": "on", "expected_status": "RECORDED"})
        deposit.refresh_from_db()
        self.assertEqual(deposit.status, "VERIFIED")
        self.client.force_login(self.root)
        url = reverse("administration:video_reward", args=[self.campaign.pk])
        self.client.post(url, {"action": "commit", "amount": "1000"})
        self.client.post(url, {"action": "draft", "kind": "TZS", "reward_amount": "50"})
        page = self.client.get(url).content.decode()
        self.assertIn("TZS 1,000", page)
        self.assertIn("Draft awaiting approval", self.client.get(reverse("administration:video_rewards")).content.decode())
        self.client.force_login(self.member)
        self.assertNotEqual(self.client.get(reverse("administration:video_rewards")).status_code, 200)

    def test_member_pages_in_english_and_swahili(self):
        self.fund(amount=1_000)
        self.tzs_policy(amount=100)
        self.client.force_login(self.member)
        listing = self.client.get(reverse("viewing:list")).content.decode()
        self.assertIn("TZS 100 per verified view", listing)
        session = self.verified_view()
        page = self.client.get(reverse("viewing:session", args=[session.pk])).content.decode()
        self.assertIn("TZS 100 reward pending", page)
        wallet = self.client.get(reverse("wallets:wallet")).content.decode()
        self.assertIn("Pending rewards", wallet)
        self.assertIn("Withdrawals are not available yet", wallet)
        self.assertNotIn("credits", wallet.split("wallet-transactions")[0].lower().split("non-cash")[0][-50:])
        with translation.override("sw"):
            notice = inbox.localize([Notification.objects.get(recipient=self.member, kind="earnings.pending")])[0]
        self.assertIn("TZS 100", notice.title)
        self.assertNotEqual(notice.title, "Reward pending: TZS 100")


class JobsTests(FinanceTestCase):
    def test_jobs_are_registered_and_scheduled(self):
        from apps.core.jobs.registry import get_job_type
        from apps.core.jobs.schedules import SCHEDULES

        for name in ("earnings.settle_rewards", "earnings.clear_rewards", "earnings.reconcile"):
            self.assertIsNotNone(get_job_type(name))
            self.assertIn(name, SCHEDULES)
        self.fund(amount=1_000)
        self.tzs_policy(amount=100)
        session = self.start()
        self.play(session, end=False)
        self.clock.advance(400)
        viewing.expire_sessions()  # Closed by the job without on-commit settlement.
        self.assertEqual(services.settle_due()["pending"], 1)
        self.assertEqual(services.reconcile()["findings"], 0)

