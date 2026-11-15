"""
CR-01: configurable campaign credits and plan eligibility (non-cash credits).

Staff set eligible plans and the total credit budget (audited); credits per verified
view stay a four-eyes policy. The credits offered are fixed when a session starts and
are exactly what a verified view earns, while the budget lasts. One credited view per
member per campaign per local day. Library videos earn nothing. Past awards never change.
"""

from datetime import timedelta
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.memberships import services as memberships
from apps.rewards import services
from apps.rewards.models import CampaignCreditTerms
from apps.rewards.terms import effective_terms
from apps.viewing import services as viewing

from .helpers import DemoReward, RewardsTestCase, User, WatchSession, make_user
from .test_concurrency import RewardConcurrencyTests


class CreditsTestCase(RewardsTestCase):
    """A Premium plan beside the default Monthly one, and a helper to set credit terms."""

    def setUp(self):
        super().setUp()
        self.premium = self.make_plan("premium", price=15_000, days=30, name="Premium",
                                      entitlements=["videos.standard", "videos.premium"])

    def terms(self, campaign=None, budget=1_000, plans=(), version=None):
        campaign = campaign or self.campaign
        if version is None:
            current = CampaignCreditTerms.objects.filter(campaign=campaign).first()
            version = current.version if current else 0
        return services.update_credit_terms(self.root, campaign, credit_budget=budget,
                                            plan_ids=[p.pk for p in plans], expected_version=version)


class CreditTermsTests(CreditsTestCase):
    def test_existing_campaign_terms_follow_the_approved_campaign_until_staff_change_them(self):
        terms = effective_terms(self.campaign)
        self.assertEqual((terms.credit_budget, terms.plan_ids, terms.configured),
                         (self.campaign.demo_budget_amount, frozenset(), False))
        self.terms(budget=500, plans=[self.premium])
        self.campaign.refresh_from_db()
        terms = effective_terms(self.campaign)
        self.assertEqual((terms.credit_budget, terms.plan_ids, terms.configured), (500, {self.premium.pk}, True))

    def test_changes_are_audited_with_before_and_after(self):
        self.terms(budget=500, plans=[self.premium])
        event = AuditEvent.objects.get(action="rewards.credit_terms.updated")
        self.assertEqual(event.actor_id, self.root.pk)
        self.assertEqual({k: event.metadata[k] for k in ("budget_from", "budget_to", "plans_from", "plans_to")},
                         {"budget_from": 100_000, "budget_to": 500, "plans_from": [], "plans_to": ["premium"]})
        policy = services.create_policy(self.root, self.campaign, reward_amount=40)
        services.approve_policy(self.approver, policy)
        self.assertTrue(AuditEvent.objects.filter(action="rewards.policy.approved", metadata__amount=40).exists())

    def test_permission_stale_edit_and_limits(self):
        member = make_user("plain@example.com")
        with self.assertRaises(services.NotAuthorized):
            services.update_credit_terms(member, self.campaign, credit_budget=1, plan_ids=[], expected_version=0)
        self.terms(budget=500)
        with self.assertRaises(services.StaleDecision):
            self.terms(budget=600, version=0)
        self.approved_policy(amount=100)
        services.evaluate_session(self.verified_session().pk)
        with self.assertRaisesMessage(ValidationError, "already awarded"):
            self.terms(budget=50)

    def test_same_video_has_different_credits_in_different_campaigns(self):
        other = self.running_campaign(name="Second", demo_reward_per_view=500)
        self.approved_policy(amount=100)
        self.approved_policy(campaign=other, amount=400)
        self.assertEqual(self.campaign.video_id, other.video_id)
        self.assertEqual((services.current_offer(self.campaign)[1], services.current_offer(other)[1]), (100, 400))

    def test_staff_page_shows_rules_and_remaining_budget(self):
        self.approved_policy(amount=100)
        self.terms(budget=300, plans=[self.plan])
        self.client.force_login(self.root)
        page = self.client.get(reverse("administration:campaign_credits", args=[self.campaign.pk]))
        for text in ("100 credits", "300 credits", "Monthly", "Remaining", "policy v1"):
            self.assertContains(page, text)
        response = self.client.post(reverse("administration:campaign_credits", args=[self.campaign.pk]),
                                    {"credit_budget": "400", "plans": [str(self.premium.pk)], "expected_version": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CampaignCreditTerms.objects.get(campaign=self.campaign).credit_budget, 400)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("administration:campaign_credits",
                                                 args=[self.campaign.pk])).status_code, 302)


class BudgetBoundsTests(CreditsTestCase):
    """awarded credits <= staff credit budget <= advertiser-approved campaign budget (100,000 here)."""

    def test_staff_budget_boundaries(self):
        self.terms(budget=100_000)  # Equal to the approved total: allowed.
        with self.assertRaisesMessage(ValidationError, "advertiser-approved campaign budget of 100000 credits"):
            self.terms(budget=100_001)
        self.approved_policy(amount=100)
        services.evaluate_session(self.verified_session().pk)  # 100 awarded.
        self.terms(budget=100)  # Equal to the awarded credits: allowed.
        with self.assertRaisesMessage(ValidationError, "already awarded"):
            self.terms(budget=99)
        self.assertEqual(CampaignCreditTerms.objects.get(campaign=self.campaign).credit_budget, 100)

    def test_database_guards_both_bounds(self):
        self.approved_policy(amount=100)
        services.evaluate_session(self.verified_session().pk)
        self.terms(budget=500)
        for value, message in ((100_001, "exceeds the advertiser-approved"), (99, "below the credits already awarded")):
            with self.subTest(value=value), self.assertRaisesMessage(DatabaseError, message), transaction.atomic():
                CampaignCreditTerms.objects.filter(campaign=self.campaign).update(credit_budget=value)

    def test_staff_see_a_clear_message(self):
        self.client.force_login(self.root)
        url = reverse("administration:campaign_credits", args=[self.campaign.pk])
        response = self.client.post(url, {"credit_budget": "100001", "expected_version": "0"}, follow=True)
        self.assertContains(response, "cannot be more than the advertiser-approved campaign budget of 100000 credits")
        self.assertFalse(CampaignCreditTerms.objects.filter(campaign=self.campaign).exists())

    def reopen(self):
        from apps.advertising import services as advertising

        campaign = advertising.pause_campaign(self.owner, self.campaign)
        return advertising.reopen_campaign(self.owner, campaign)

    def test_approved_budget_change_lowers_the_credit_terms(self):
        from apps.advertising import services as advertising

        self.approved_policy(amount=100)
        services.evaluate_session(self.verified_session().pk)
        other = self.new_member("second@example.com")
        services.evaluate_session(self.verified_session(member=other).pk)  # 200 awarded.
        self.terms(budget=80_000)
        campaign = self.reopen()
        with self.assertRaisesMessage(ValidationError, "200 credits already awarded"):
            advertising.update_campaign(self.owner, campaign, {"demo_budget_amount": 150},
                                        expected_revision=campaign.revision)
        campaign = advertising.update_campaign(self.owner, campaign, {"demo_budget_amount": 50_000},
                                               expected_revision=campaign.revision)
        # A draft does not change the active terms yet.
        self.assertEqual(CampaignCreditTerms.objects.get(campaign=campaign).credit_budget, 80_000)
        campaign = advertising.submit_campaign(self.owner, campaign)
        campaign = advertising.approve_campaign(self.root, campaign, expected_revision=campaign.revision)
        terms = CampaignCreditTerms.objects.get(campaign=campaign)
        self.assertEqual((terms.credit_budget, campaign.demo_budget_amount), (50_000, 50_000))
        event = AuditEvent.objects.filter(action="rewards.credit_terms.updated").order_by("-occurred_at").first()
        self.assertEqual((event.metadata["budget_from"], event.metadata["budget_to"], event.metadata["reason"]),
                         (80_000, 50_000, "approved_campaign_budget"))
        self.assertEqual(services.budget_summary(campaign)["demo_consumed"], 200)  # Past awards untouched.

    def test_terms_below_the_new_budget_are_left_alone(self):
        from apps.advertising import services as advertising

        self.terms(budget=20_000)
        campaign = self.reopen()
        campaign = advertising.update_campaign(self.owner, campaign, {"demo_budget_amount": 50_000},
                                               expected_revision=campaign.revision)
        campaign = advertising.submit_campaign(self.owner, campaign)
        advertising.approve_campaign(self.root, campaign, expected_revision=campaign.revision)
        self.assertEqual(CampaignCreditTerms.objects.get(campaign=campaign).credit_budget, 20_000)

    def test_database_refuses_approving_a_budget_below_active_terms(self):
        from apps.advertising import services as advertising

        self.terms(budget=80_000)
        campaign = self.reopen()
        campaign = advertising.update_campaign(self.owner, campaign, {"demo_budget_amount": 50_000},
                                               expected_revision=campaign.revision)
        campaign = advertising.submit_campaign(self.owner, campaign)
        # Approval that skips the service step that lowers the terms:
        with self.assertRaisesMessage(DatabaseError, "below its active credit budget"), transaction.atomic():
            type(campaign).objects.filter(pk=campaign.pk).update(
                status="SCHEDULED", approved_revision=campaign.revision, reviewed_at=timezone.now(),
                reviewed_by=self.root)


class PlanEligibilityTests(CreditsTestCase):
    def test_ineligible_plan_is_locked_with_the_qualifying_plans(self):
        self.approved_policy(amount=100)
        self.terms(plans=[self.premium])
        with self.assertRaises(viewing.Denied) as denied:
            self.start()
        self.assertEqual(denied.exception.code, "plan_not_targeted")
        offers = viewing.campaign_offers(self.member)
        self.assertEqual([(o["locked"], o["plans"], o["credits"]) for o in offers], [(True, ["Premium"], 100)])
        self.client.force_login(self.member)
        page = self.client.get(reverse("viewing:campaign", args=[self.campaign.pk]))
        self.assertContains(page, "This campaign is for members on: Premium.")
        self.assertNotContains(page, "Start watching")

    def test_eligible_plan_sees_credits_and_requirements(self):
        self.approved_policy(amount=100)
        self.terms(plans=[self.plan, self.premium])
        self.client.force_login(self.member)
        page = self.client.get(reverse("viewing:campaign", args=[self.campaign.pk]))
        self.assertContains(page, "Earn 100 credits per verified view")
        self.assertContains(page, "Watch at least")
        self.assertContains(page, "One credited view per day")
        self.assertContains(page, "Start watching")
        listing = self.client.get(reverse("viewing:list"))
        self.assertContains(listing, "100 credits per verified view")


class AwardConsistencyTests(CreditsTestCase):
    def test_rate_is_fixed_when_the_session_starts(self):
        self.approved_policy(amount=60)
        session = self.start()
        self.assertEqual(session.quoted_credits, 60)
        self.event(session, 1, 0)
        self.approved_policy(amount=100)  # Staff raise the rate during playback.
        self.watch(session, 95, start_seq=2)
        reward = services.evaluate_session(session.pk)
        self.assertEqual((reward.status, reward.amount, reward.policy_version), ("DEMO_QUALIFIED", 60, 1))
        self.assertEqual(services.current_offer(self.campaign)[1], 100)  # New sessions get the new rate.
        self.clock.advance(24 * 3600)
        later = self.watch(self.start(campaign=self.running_campaign(name="Other", demo_reward_per_view=500)), 95)
        self.assertEqual(later.quoted_credits, 0)  # No policy there: shown and earns nothing.

    def test_policy_approved_after_start_does_not_add_credits(self):
        session = self.start()
        self.assertEqual(session.quoted_credits, 0)
        self.approved_policy(amount=100)
        reward = services.evaluate_session(self.watch(session, 95).pk)
        self.assertEqual((reward.status, reward.reasons), ("DEMO_REJECTED", ["no_policy"]))

    def test_quote_cannot_be_changed_in_the_database(self):
        self.approved_policy(amount=100)
        session = self.start()
        with self.assertRaises(DatabaseError), transaction.atomic():
            WatchSession.objects.filter(pk=session.pk).update(quoted_credits=999)

    def test_incomplete_view_earns_nothing(self):
        self.approved_policy(amount=100)
        session = self.watch(self.start(), 30)
        self.assertNotEqual(session.outcome, WatchSession.Outcome.VERIFIED_EVIDENCE)
        services.evaluate_pending()
        self.assertFalse(DemoReward.objects.filter(status="DEMO_QUALIFIED").exists())

    def test_same_day_repeat_is_refused_next_day_allowed(self):
        campaign = self.running_campaign(name="Daily", max_views_per_member=3)
        self.approved_policy(campaign=campaign, amount=50)
        self.watch(self.start(campaign=campaign), 95)
        with self.assertRaises(viewing.Denied) as denied:
            self.start(campaign=campaign)
        self.assertEqual(denied.exception.code, "daily_limit")
        self.clock.advance(24 * 3600)
        self.assertEqual(self.watch(self.start(campaign=campaign), 95).outcome, WatchSession.Outcome.VERIFIED_EVIDENCE)

    def test_retries_award_once_and_past_awards_never_change(self):
        self.approved_policy(amount=60)
        session = self.verified_session()
        first = services.evaluate_session(session.pk)
        again = services.evaluate_session(session.pk)
        self.assertEqual((first.pk, DemoReward.objects.count()), (again.pk, 1))
        self.approved_policy(amount=90)
        self.terms(budget=5_000, plans=[self.premium])
        first.refresh_from_db()
        self.assertEqual((first.amount, first.status), (60, "DEMO_QUALIFIED"))
        self.client.force_login(self.member)
        history = self.client.get(reverse("rewards:history"))
        self.assertContains(history, "60 credits")
        self.assertContains(history, self.campaign.name)

    def test_budget_never_overspent(self):
        self.approved_policy(amount=100)
        self.terms(budget=150)
        first = services.evaluate_session(self.verified_session().pk)
        other = self.new_member("second@example.com")
        second = services.evaluate_session(self.verified_session(member=other).pk)
        self.assertEqual((first.status, second.status, second.reasons),
                         ("DEMO_QUALIFIED", "DEMO_REJECTED", ["budget_exhausted"]))
        self.assertEqual(services.budget_summary(self.campaign)["demo_consumed"], 100)

    def test_library_videos_earn_no_credits(self):
        from apps.videos import services as videos

        library = self.published_video("Library clip")
        reason, _playback = videos.authorize_playback(self.member, library)
        self.assertEqual(reason, "ok")
        services.evaluate_pending()
        self.assertFalse(DemoReward.objects.exists())
        self.assertFalse(WatchSession.objects.filter(video=library).exists())


@skipUnless(connection.vendor == "postgresql", "Concurrency is verified on PostgreSQL.")
class QuotedBudgetConcurrencyTests(RewardConcurrencyTests):
    """Concurrent awards against a staff credit budget (quoted sessions, PostgreSQL threads).
    Setup: approved campaign budget 300, approved policy 100 credits per view."""

    def quoted_sessions(self, count, prefix):
        policy, credits = services.current_offer(self.campaign)
        return [quoted_verified_session(self.member(f"{prefix}{i}@x.example"), self.campaign, policy, credits)
                for i in range(count)]

    def test_four_concurrent_awards_never_exceed_the_budget(self):
        from apps.memberships.tests.test_concurrency import run_threads

        services.update_credit_terms(self.root, self.campaign, credit_budget=250, plan_ids=[], expected_version=0)
        sessions = self.quoted_sessions(4, "q")
        # Nothing awarded yet; 250 fits two awards of 100. All four race; two must lose.
        run_threads(4, lambda i: services.evaluate_session(sessions[i].pk))
        self.assertEqual(DemoReward.objects.filter(status="DEMO_QUALIFIED").count(), 2)
        self.assertEqual(services.budget_summary(self.campaign)["demo_consumed"], 200)
        run_threads(4, lambda i: services.evaluate_session(sessions[i].pk))  # Retries change nothing.
        self.assertEqual(DemoReward.objects.count(), 4)
        self.assertEqual(services.budget_summary(self.campaign)["demo_consumed"], 200)

    def test_prior_awards_leave_exactly_one_award_for_four_racers(self):
        from apps.memberships.tests.test_concurrency import run_threads

        services.update_credit_terms(self.root, self.campaign, credit_budget=300, plan_ids=[], expected_version=0)
        for session in self.quoted_sessions(2, "prior"):  # 200 of 300 already awarded.
            self.assertEqual(services.evaluate_session(session.pk).status, "DEMO_QUALIFIED")
        self.assertEqual(services.budget_summary(self.campaign),
                         {"demo_allocated": 300, "demo_consumed": 200, "demo_remaining": 100})
        racers = self.quoted_sessions(4, "racer")
        run_threads(4, lambda i: services.evaluate_session(racers[i].pk))
        decisions = DemoReward.objects.filter(session__in=racers)
        self.assertEqual(decisions.filter(status="DEMO_QUALIFIED").count(), 1)
        self.assertEqual(decisions.filter(status="DEMO_REJECTED", reasons=["budget_exhausted"]).count(), 3)
        self.assertEqual(services.budget_summary(self.campaign)["demo_consumed"], 300)
        before = list(DemoReward.objects.order_by("pk").values_list("pk", "status", "amount"))
        run_threads(4, lambda i: services.evaluate_session(racers[i].pk))  # Retries.
        self.assertEqual(list(DemoReward.objects.order_by("pk").values_list("pk", "status", "amount")), before)
        self.assertEqual(services.budget_summary(self.campaign)["demo_consumed"], 300)


    # Inherit only the setup; the parent's own tests run in test_concurrency.
    test_parallel_evaluation_records_one_decision = None
    test_concurrent_budget_exhaustion = None
    test_parallel_jobs_and_reversals_release_once = None


def quoted_verified_session(member, campaign, policy, credits):
    """Like test_concurrency.verified_session, with the offer recorded at creation (CR-01)."""
    now = timezone.now()
    session = WatchSession.objects.create(member=member, campaign=campaign, campaign_revision=campaign.revision,
                                          video=campaign.video, start_deadline=now + timedelta(minutes=5),
                                          expires_at=now + timedelta(hours=1), video_duration_ms=95_000,
                                          required_ms=85_500, quoted_policy=policy, quoted_credits=credits)
    WatchSession.objects.filter(pk=session.pk).update(state="IN_PROGRESS", started_at=now, credited_ms=90_000,
                                                      coverage_ms=90_000, event_count=10, last_sequence=10)
    WatchSession.objects.filter(pk=session.pk).update(state="FINALIZED", outcome="VERIFIED_EVIDENCE",
                                                      qualifying_slot=1, finalized_at=now + timedelta(seconds=1),
                                                      reasons=["thresholds_met"])
    return WatchSession.objects.get(pk=session.pk)


del RewardConcurrencyTests  # Imported for its setup only; collected in test_concurrency.
