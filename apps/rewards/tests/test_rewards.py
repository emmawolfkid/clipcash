import inspect
from datetime import timedelta

from django.db import DatabaseError, IntegrityError, transaction
from django.test import override_settings

from apps.accounts.tests.helpers import run_worker
from apps.advertising import services as advertising
from apps.audit.models import AuditEvent
from apps.core.jobs.services import enqueue
from apps.rewards import services
from apps.rewards.models import CampaignRewardBudget
from apps.viewing import services as viewing

from .helpers import DemoReward, RewardsTestCase, WatchSession

R = DemoReward.Status


@override_settings(VIEWING_MAX_STARTS_PER_HOUR=100)
class EvaluationTests(RewardsTestCase):
    def test_qualifies_once_with_policy_amount(self):
        self.approved_policy(amount=80)
        session = self.verified_session()
        reward = services.evaluate_session(session.pk)
        self.assertEqual((reward.status, reward.amount, reward.policy_version, reward.campaign_revision),
                         (R.QUALIFIED, 80, 1, session.campaign_revision))
        self.assertEqual(services.budget_summary(self.campaign),
                         {"demo_allocated": 100_000, "demo_consumed": 80, "demo_remaining": 99_920})
        again = services.evaluate_session(session.pk, actor=self.root)
        self.assertEqual(again.pk, reward.pk)
        self.assertTrue(AuditEvent.objects.filter(action="rewards.duplicate.refused").exists())
        self.assertEqual(DemoReward.objects.count(), 1)
        self.assertEqual(services.budget_summary(self.campaign)["demo_consumed"], 80)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DemoReward.objects.create(session=session, member=self.member, campaign=self.campaign,
                                      campaign_revision=1, amount=0, status=R.REJECTED, decided_at=session.finalized_at)

    def test_no_policy_or_unverified_views_are_rejected(self):
        session = self.verified_session()
        self.assertEqual(services.evaluate_session(session.pk).reasons, ["no_policy"])
        self.approved_policy()
        other = self.new_member("short@x.example")
        short = self.start(member=other)
        self.event(short, 1, 0, member=other)
        viewing.finish_session(other, short.pk)
        self.assertNotIn(short.pk, list(services.pending_sessions()))
        self.assertEqual(services.evaluate_session(short.pk).reasons, ["session_not_verified"])

    def test_policy_in_force_at_the_view_applies(self):
        self.approved_policy(amount=100)
        session = self.verified_session()
        self.clock.advance(60)
        services.approve_policy(self.approver, services.create_policy(self.root, self.campaign, reward_amount=40))
        self.assertEqual(services.evaluate_session(session.pk).amount, 100)  # Not re-priced by v2.

    def test_waiting_states_record_nothing(self):
        self.approved_policy()
        session = self.verified_session()
        advertising.review_organization(self.root, self.org, "suspend", expected_status="APPROVED", reason="Check")
        self.assertIsNone(services.evaluate_session(session.pk))
        self.assertEqual(services.evaluate_pending(), {"qualified": 0, "rejected": 0, "waiting": 1})
        advertising.review_organization(self.root, self.org, "reinstate", expected_status="SUSPENDED")
        self.assertEqual(services.evaluate_session(session.pk).status, R.QUALIFIED)

    def test_completed_campaign_keeps_rewards_but_cancelled_does_not(self):
        self.approved_policy()
        session = self.verified_session()
        advertising.run_schedule(now=self.campaign.ends_at + timedelta(minutes=1))
        self.assertEqual(services.evaluate_session(session.pk).status, R.QUALIFIED)
        other_campaign = self.running_campaign(name="Cancelled later")
        self.approved_policy(campaign=other_campaign)
        late = self.verified_session(campaign=other_campaign)
        advertising.cancel_campaign(self.owner, other_campaign)
        self.assertEqual(services.evaluate_session(late.pk).reasons, ["campaign_cancelled"])

    def test_budget_is_never_exceeded(self):
        campaign = self.running_campaign(name="Small budget", demo_budget_amount=250, demo_reward_per_view=100)
        self.approved_policy(campaign=campaign, amount=100)
        sessions = [self.verified_session(member=self.new_member(f"b{i}@x.example"), campaign=campaign)
                    for i in range(3)]
        results = [services.evaluate_session(s.pk) for s in sessions]
        self.assertEqual([r.status for r in results], [R.QUALIFIED, R.QUALIFIED, R.REJECTED])
        self.assertEqual(results[2].reasons, ["budget_exhausted"])
        self.assertEqual(services.budget_summary(campaign),
                         {"demo_allocated": 250, "demo_consumed": 200, "demo_remaining": 50})
        self.assertTrue(AuditEvent.objects.filter(action="rewards.budget.exhausted").exists())
        with self.assertRaises(DatabaseError), transaction.atomic():
            CampaignRewardBudget.objects.filter(campaign=campaign).update(demo_consumed=300)

    def test_suspicious_views_wait_for_review(self):
        self.approved_policy()
        session = self.verified_session()
        # Simulate a pending review state by checking eligibility of a pending session copy.
        session.outcome, session.review_status = WatchSession.Outcome.SUSPICIOUS_REVIEW, "PENDING"
        self.assertTrue(services.reward_eligibility(session).retry_later)


class ReversalTests(RewardsTestCase):
    def setUp(self):
        super().setUp()
        self.approved_policy(amount=100)
        self.session = self.verified_session()
        self.reward = services.evaluate_session(self.session.pk)

    def test_revoking_the_view_reverses_once(self):
        viewing.revoke_verified_session(self.root, self.session, reason="Scripted playback found")
        self.reward.refresh_from_db()
        self.session.refresh_from_db()
        self.assertEqual((self.reward.status, self.reward.amount), (R.REVERSED, 100))
        self.assertEqual((self.session.outcome, self.session.qualifying_slot), ("REJECTED", None))
        self.assertEqual(services.budget_summary(self.campaign)["demo_consumed"], 0)
        viewing.revoke_verified_session(self.root, self.session, reason="Repeated click")
        self.assertEqual(services.reconcile_reversals(), 0)
        self.assertEqual(services.budget_summary(self.campaign)["demo_consumed"], 0)
        self.assertEqual(AuditEvent.objects.filter(action="rewards.reward.reversed").count(), 1)
        with self.assertRaises(DatabaseError), transaction.atomic():
            DemoReward.objects.filter(pk=self.reward.pk).update(status=R.QUALIFIED, reversed_at=None)

    def test_reconciliation_backstop(self):
        viewing.session_reversed.disconnect(dispatch_uid="rewards.reverse_on_session_reversed")
        try:
            viewing.revoke_verified_session(self.root, self.session, reason="Signal lost in test")
        finally:
            viewing.session_reversed.connect(services.on_session_reversed,
                                             dispatch_uid="rewards.reverse_on_session_reversed")
        self.reward.refresh_from_db()
        self.assertEqual(self.reward.status, R.QUALIFIED)
        self.assertEqual(services.reconcile_reversals(), 1)
        self.assertEqual(services.reconcile_reversals(), 0)
        self.reward.refresh_from_db()
        self.assertEqual(self.reward.status, R.REVERSED)
        self.assertEqual(services.budget_summary(self.campaign)["demo_consumed"], 0)

    def test_administrative_reversal(self):
        with self.assertRaises(services.NotAuthorized):
            services.reverse_reward(self.manager, self.reward, reason="Not allowed")
        with self.assertRaises(Exception):
            services.reverse_reward(self.root, self.reward, reason="")
        services.reverse_reward(self.root, self.reward, reason="Duplicate account")
        services.reverse_reward(self.root, self.reward, reason="Again", expected_status=R.REVERSED)
        with self.assertRaises(services.StaleDecision):
            services.reverse_reward(self.root, self.reward, reason="Stale view")
        self.assertEqual(services.budget_summary(self.campaign)["demo_consumed"], 0)

    def test_decisions_are_immutable(self):
        for change in ({"amount": 999}, {"policy_version": 5}, {"campaign_revision": 4}):
            with self.subTest(change=change), self.assertRaises(DatabaseError), transaction.atomic():
                DemoReward.objects.filter(pk=self.reward.pk).update(**change)
        with self.assertRaises(DatabaseError), transaction.atomic():
            DemoReward.objects.filter(pk=self.reward.pk).delete()


class FirewallAndJobTests(RewardsTestCase):
    def test_no_ledger_or_funding_changes(self):
        from apps.wallets.models import JournalEntry, JournalLine, LedgerAccount

        before = (JournalEntry.objects.count(), JournalLine.objects.count(), LedgerAccount.objects.count())
        self.approved_policy()
        reward = services.evaluate_session(self.verified_session().pk)
        services.reverse_reward(self.root, reward, reason="Firewall test")
        self.assertEqual((JournalEntry.objects.count(), JournalLine.objects.count(), LedgerAccount.objects.count()),
                         before)
        self.campaign.refresh_from_db()
        self.assertEqual((self.campaign.funding_status, self.campaign.financially_eligible), ("UNFUNDED", False))
        import apps.rewards.models as models_module

        for module in (services, models_module):
            source = inspect.getsource(module)
            self.assertNotRegex(source, r"(?m)^\s*(from|import) apps\.wallets")

    def test_job_is_idempotent(self):
        self.approved_policy()
        session = self.verified_session()
        enqueue("rewards.evaluate_pending", {})
        run_worker()
        enqueue("rewards.evaluate_pending", {}, idempotency_key="replay")
        run_worker()
        self.assertEqual(DemoReward.objects.filter(session=session).count(), 1)
        enqueue("rewards.reconcile_reversals", {})
        run_worker()
        self.assertEqual(DemoReward.objects.get(session=session).status, R.QUALIFIED)

    def test_audit_has_no_personal_data(self):
        self.approved_policy()
        services.evaluate_session(self.verified_session().pk)
        for event in AuditEvent.objects.filter(category="REWARDS"):
            self.assertNotIn("@", str(event.metadata) + event.target_repr)
