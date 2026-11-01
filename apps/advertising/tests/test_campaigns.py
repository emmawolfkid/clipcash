from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.utils import timezone

from apps.accounts.tests.helpers import run_worker
from apps.advertising import services
from apps.advertising.models import Campaign
from apps.audit.models import AuditEvent
from apps.memberships import services as memberships
from apps.videos import services as videos

from .helpers import AdvertisingTestCase, make_user

S = Campaign.Status


class CampaignValidationTests(AdvertisingTestCase):
    def test_requires_authorized_video(self):
        other = self.published_video("Not granted")
        with self.assertRaises(ValidationError) as caught:
            self.make_campaign(video=other)
        self.assertIn("video", caught.exception.message_dict)
        grant = self.org.video_grants.get()
        services.revoke_video_grant(self.root, self.org, grant.pk)
        with self.assertRaises(ValidationError):
            self.make_campaign()

    def test_dates(self):
        now = timezone.now()
        cases = {
            "ends_at": [{"starts_at": now + timedelta(days=2), "ends_at": now + timedelta(days=1)},
                        {"starts_at": now - timedelta(days=3), "ends_at": now - timedelta(days=1)},
                        {"starts_at": now, "ends_at": now + timedelta(days=400)}],
            "starts_at": [{"starts_at": now.replace(tzinfo=None)}],
        }
        for field, variants in cases.items():
            for values in variants:
                with self.subTest(values=values), self.assertRaises(ValidationError) as caught:
                    self.make_campaign(**values)
                self.assertIn(field, caught.exception.message_dict)

    def test_demo_amounts_are_whole_positive_shillings(self):
        for values in ({"demo_budget_amount": 1000.5}, {"demo_budget_amount": Decimal("10.50")},
                       {"demo_budget_amount": 0}, {"demo_budget_amount": -5}, {"demo_reward_per_view": True},
                       {"demo_budget_amount": 100, "demo_reward_per_view": 101}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.make_campaign(**values)
        campaign = self.make_campaign(demo_budget_amount=Decimal("250000"), demo_reward_per_view=Decimal("250"))
        self.assertEqual((campaign.demo_budget_amount, campaign.demo_view_capacity), (250_000, 1000))

    def test_targeting_only_active_plans(self):
        archived = memberships.set_plan_status(self.root, self.make_plan("old"), "ARCHIVED")
        with self.assertRaises(ValidationError):
            self.make_campaign(target_plans=[archived])
        campaign = self.make_campaign(target_plans=[self.plan])
        self.assertEqual(list(campaign.target_plans.all()), [self.plan])

    def test_cannot_create_for_suspended_or_foreign_org(self):
        stranger = make_user("stranger@x.example")
        with self.assertRaises(services.NotAuthorized):
            self.make_campaign(user=stranger)
        services.review_organization(self.root, self.org, "suspend", expected_status="APPROVED", reason="Review")
        self.org.refresh_from_db()
        with self.assertRaises(services.InvalidState):
            self.make_campaign()


class LifecycleTests(AdvertisingTestCase):
    def test_submit_approve_activates_immediately_when_started(self):
        campaign = self.make_campaign()
        self.assertEqual((campaign.status, campaign.revision, campaign.funding_status), (S.DRAFT, 1, "UNFUNDED"))
        campaign = services.submit_campaign(self.owner, campaign)
        campaign = services.approve_campaign(self.root, campaign, expected_revision=1, notes="Looks good")
        self.assertEqual((campaign.status, campaign.approved_revision, campaign.reviewed_by),
                         (S.ACTIVE, 1, self.root))
        self.assertIsNotNone(campaign.activated_at)
        actions = list(AuditEvent.objects.filter(target_id=str(campaign.pk)).order_by("occurred_at")
                       .values_list("action", flat=True))
        self.assertEqual(actions, ["campaign.created", "campaign.submitted", "campaign.approved", "campaign.activated"])

    def test_pending_org_cannot_submit(self):
        applicant = make_user("p@x.example")
        org = self.make_org(applicant, "Pending")
        services.grant_video(self.root, org, self.ad_video)
        campaign = self.make_campaign(user=applicant, org=org)
        with self.assertRaises(services.InvalidState) as caught:
            services.submit_campaign(applicant, campaign)
        self.assertEqual(caught.exception.code, "organization_not_approved")

    def test_future_campaign_is_scheduled_then_activated_and_completed_by_job(self):
        now = timezone.now()
        campaign = services.submit_campaign(self.owner, self.make_campaign(
            starts_at=now + timedelta(hours=2), ends_at=now + timedelta(days=1)))
        campaign = services.approve_campaign(self.root, campaign, expected_revision=1)
        self.assertEqual(campaign.status, S.SCHEDULED)
        self.assertEqual(services.run_schedule(now=now), {"activated": 0, "completed": 0})
        self.assertEqual(services.run_schedule(now=now + timedelta(hours=3)), {"activated": 1, "completed": 0})
        self.assertEqual(services.run_schedule(now=now + timedelta(hours=3)), {"activated": 0, "completed": 0})
        self.assertEqual(services.run_schedule(now=now + timedelta(days=2)), {"activated": 0, "completed": 1})
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, S.COMPLETED)
        services.archive_campaign(self.owner, campaign)

    def test_scheduled_job_is_registered_and_runs(self):
        from apps.core.jobs.schedules import SCHEDULES

        self.assertIn("advertising.campaign_schedule", SCHEDULES)
        campaign = self.running_campaign()
        from apps.core.jobs.services import enqueue

        enqueue("advertising.campaign_schedule", {})
        run_worker()
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, S.ACTIVE)

    def test_rejection_edit_and_stale_approval(self):
        campaign = services.submit_campaign(self.owner, self.make_campaign())
        with self.assertRaises(ValidationError):
            services.reject_campaign(self.root, campaign, expected_revision=1, reason="")
        campaign = services.reject_campaign(self.root, campaign, expected_revision=1, reason="Use a clearer title")
        self.assertEqual((campaign.status, campaign.review_notes), (S.REJECTED, "Use a clearer title"))
        campaign = services.update_campaign(self.owner, campaign, {"name": "Clear title"}, expected_revision=1)
        self.assertEqual((campaign.status, campaign.revision), (S.DRAFT, 2))
        with self.assertRaises(services.StaleDecision):
            services.update_campaign(self.owner, campaign, {"name": "Again"}, expected_revision=1)
        campaign = services.submit_campaign(self.owner, campaign)
        with self.assertRaises(services.StaleDecision):  # Reviewer looked at revision 1.
            services.approve_campaign(self.root, campaign, expected_revision=1)
        campaign = services.approve_campaign(self.root, campaign, expected_revision=2)
        again = services.approve_campaign(self.root, campaign, expected_revision=2)  # Double click.
        self.assertEqual(again.status, S.ACTIVE)
        self.assertEqual(AuditEvent.objects.filter(action="campaign.approved", target_id=str(campaign.pk)).count(), 1)

    def test_changes_after_approval_require_new_review(self):
        campaign = self.running_campaign()
        with self.assertRaises(services.InvalidState):
            services.update_campaign(self.owner, campaign, {"demo_budget_amount": 999_999},
                                     expected_revision=campaign.revision)
        with self.assertRaises(services.InvalidState):
            services.reopen_campaign(self.owner, campaign)  # Pause first.
        campaign = services.pause_campaign(self.owner, campaign)
        campaign = services.reopen_campaign(self.owner, campaign)
        self.assertEqual((campaign.status, campaign.revision, campaign.approved_revision), (S.DRAFT, 2, 1))
        campaign = services.update_campaign(self.owner, campaign, {"demo_budget_amount": 200_000},
                                            expected_revision=2)
        self.assertEqual(campaign.revision, 3)
        self.assertNotIn(campaign.status, (S.ACTIVE, S.SCHEDULED))

    def test_staff_pause_cannot_be_lifted_by_advertiser(self):
        campaign = self.running_campaign()
        with self.assertRaises(ValidationError):
            services.pause_campaign(self.root, campaign, reason="")
        campaign = services.pause_campaign(self.root, campaign, reason="Policy check")
        self.assertTrue(campaign.paused_by_staff)
        with self.assertRaises(services.InvalidState) as caught:
            services.resume_campaign(self.owner, campaign)
        self.assertEqual(caught.exception.code, "paused_by_staff")
        campaign = services.resume_campaign(self.root, campaign)
        self.assertEqual((campaign.status, campaign.paused_by_staff), (S.ACTIVE, False))

    def test_resume_blocked_when_org_suspended_or_video_unpublished(self):
        campaign = services.pause_campaign(self.owner, self.running_campaign())
        videos.unpublish(self.manager, self.ad_video)
        with self.assertRaises(services.InvalidState) as caught:
            services.resume_campaign(self.owner, campaign)
        self.assertEqual(caught.exception.code, "video_unavailable")

    def test_cancel_and_archive(self):
        campaign = services.cancel_campaign(self.owner, self.running_campaign())
        self.assertEqual(campaign.status, S.CANCELLED)
        with self.assertRaises(services.InvalidState):
            services.resume_campaign(self.owner, campaign)
        campaign = services.archive_campaign(self.owner, campaign)
        self.assertEqual(campaign.status, S.ARCHIVED)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Campaign.objects.filter(pk=campaign.pk).update(status=S.DRAFT)

    def test_database_guards(self):
        campaign = self.running_campaign()
        with self.assertRaises(DatabaseError), transaction.atomic():  # Reviewed details are frozen.
            Campaign.objects.filter(pk=campaign.pk).update(demo_budget_amount=5_000_000)
        with self.assertRaises(IntegrityError), transaction.atomic():  # Revision bump while active is refused.
            Campaign.objects.filter(pk=campaign.pk).update(revision=2, demo_budget_amount=5_000_000)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Campaign.objects.filter(pk=campaign.pk).update(status=S.SUBMITTED)
        other = self.make_org(make_user("o@x.example"), "Other")
        with self.assertRaises(DatabaseError), transaction.atomic():
            Campaign.objects.filter(pk=campaign.pk).update(organization=other)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Campaign.objects.filter(pk=campaign.pk).delete()

    def test_reviewers_only_and_no_self_review(self):
        campaign = services.submit_campaign(self.owner, self.make_campaign())
        for actor in (self.owner, self.manager, self.member):
            with self.subTest(actor=actor.email), self.assertRaises(services.NotAuthorized):
                services.approve_campaign(actor, campaign, expected_revision=1)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, S.SUBMITTED)
        self.assertTrue(AuditEvent.objects.filter(action="advertiser.action.denied",
                                                  metadata__action="campaign.approve").exists())


class EligibilityAndFirewallTests(AdvertisingTestCase):
    def test_campaign_and_member_eligibility(self):
        campaign = self.make_campaign()
        self.assertEqual(services.campaign_eligibility(campaign).reason, "campaign_not_active")
        campaign = services.submit_campaign(self.owner, campaign)
        campaign = services.approve_campaign(self.root, campaign, expected_revision=1)
        self.assertTrue(services.campaign_eligibility(campaign))
        self.assertEqual(services.campaign_eligibility(campaign, self.member).reason, "no_membership")
        self.active_membership()
        decision = services.campaign_eligibility(campaign, self.member)
        self.assertTrue(decision)
        self.assertEqual((decision.demo_only, decision.financially_eligible), (True, False))
        self.assertEqual([c.pk for c in services.eligible_campaigns(self.member)], [campaign.pk])
        self.assertEqual(services.campaign_eligibility(campaign, None, now=campaign.ends_at).reason, "ended")

    def test_plan_targeting_and_entitlement(self):
        gold = self.make_plan("gold", price=9_000)
        targeted = self.running_campaign(target_plans=[gold])
        self.active_membership()
        self.assertEqual(services.campaign_eligibility(targeted, self.member).reason, "plan_not_targeted")
        premium_video = self.published_video("Premium ad", required_entitlement="videos.premium")
        services.grant_video(self.root, self.org, premium_video)
        premium = self.running_campaign(name="Premium", video=premium_video)
        self.assertEqual(services.campaign_eligibility(premium, self.member).reason, "entitlement_missing")

    def test_default_deny_when_dependencies_fail(self):
        campaign = self.running_campaign()

        def reason():
            return services.campaign_eligibility(Campaign.objects.get(pk=campaign.pk)).reason

        videos.unpublish(self.manager, self.ad_video)
        self.assertEqual(reason(), "video_unavailable")
        videos.publish(self.manager, self.ad_video)
        services.revoke_video_grant(self.root, self.org, self.org.video_grants.get().pk)
        self.assertEqual(reason(), "video_not_authorized")
        services.grant_video(self.root, self.org, self.ad_video)
        services.review_organization(self.root, self.org, "suspend", expected_status="APPROVED", reason="Check")
        self.assertEqual(reason(), "organization_not_approved")
        from .helpers import enable_advertisers

        enable_advertisers(False)
        self.assertEqual(services.campaign_eligibility(campaign).reason, "feature_disabled")

    def test_funding_cannot_be_verified(self):
        campaign = self.running_campaign()
        for status in ("VERIFIED", "PENDING_RECONCILIATION"):
            with self.subTest(status=status), self.assertRaises(services.FundingLocked):
                services.request_funding_status(self.root, campaign, status)
        self.assertEqual(AuditEvent.objects.filter(action="campaign.funding.refused").count(), 2)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Campaign.objects.filter(pk=campaign.pk).update(funding_status="VERIFIED")
        campaign.refresh_from_db()
        self.assertEqual(campaign.funding_status, "UNFUNDED")
        self.assertFalse(campaign.financially_eligible)

    def test_no_ledger_activity_and_member_playback_unchanged(self):
        from apps.wallets.models import JournalEntry, JournalLine, LedgerAccount

        before = (JournalEntry.objects.count(), JournalLine.objects.count(), LedgerAccount.objects.count())
        campaign = self.running_campaign(demo_budget_amount=10_000_000)
        services.run_schedule(now=campaign.ends_at + timedelta(minutes=1))
        self.assertEqual((JournalEntry.objects.count(), JournalLine.objects.count(), LedgerAccount.objects.count()),
                         before)
        # Phase 04 playback still needs a membership, campaign or not.
        reason, playback = videos.authorize_playback(self.member, self.ad_video)
        self.assertEqual((reason, playback), ("no_membership", None))

    def test_sensitive_data_not_audited(self):
        self.running_campaign()
        for event in AuditEvent.objects.filter(category="ADVERTISING"):
            text = str(event.metadata) + event.target_repr
            self.assertNotIn("TIN-123", text)
            self.assertNotIn("ads@example.com", text)
