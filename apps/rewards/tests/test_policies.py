from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction

from apps.advertising import services as advertising
from apps.audit.models import AuditEvent
from apps.rewards import services

from .helpers import RewardPolicy, RewardsTestCase

P = RewardPolicy.Status


class PolicyTests(RewardsTestCase):
    def test_draft_and_four_eyes_approval(self):
        policy = services.create_policy(self.root, self.campaign, reward_amount=80, notes="Launch")
        self.assertEqual((policy.status, policy.version, policy.campaign_revision, policy.reward_amount),
                         (P.DRAFT, 1, self.campaign.approved_revision, 80))
        with self.assertRaises(services.NotAuthorized) as caught:
            services.approve_policy(self.root, policy)  # Author cannot approve.
        self.assertEqual(caught.exception.code, "four_eyes")
        policy = services.approve_policy(self.approver, policy)
        self.assertEqual((policy.status, policy.decided_by), (P.APPROVED, self.approver))
        self.assertIsNotNone(policy.effective_from)
        services.approve_policy(self.approver, policy, expected_status=P.APPROVED)  # Repeat: no-op.
        self.assertEqual(AuditEvent.objects.filter(action="rewards.policy.approved").count(), 1)

    def test_amount_validation(self):
        for amount in (0, -5, 1.5, Decimal("10.5"), True, "abc", 101):  # Campaign allows TZS 100 per view.
            with self.subTest(amount=amount), self.assertRaises(ValidationError):
                services.create_policy(self.root, self.campaign, reward_amount=amount)
        self.assertEqual(services.create_policy(self.root, self.campaign, reward_amount=Decimal("100")).reward_amount,
                         100)

    def test_permissions(self):
        for actor in (self.manager, self.member, self.owner):
            with self.subTest(actor=actor.email), self.assertRaises(services.NotAuthorized):
                services.create_policy(actor, self.campaign, reward_amount=50)
        self.assertTrue(AuditEvent.objects.filter(action="rewards.action.denied").exists())
        policy = services.create_policy(self.root, self.campaign, reward_amount=50)
        with self.assertRaises(services.NotAuthorized):
            services.approve_policy(self.manager, policy)

    def test_campaign_must_be_approved(self):
        draft = self.make_campaign(name="Draft campaign")
        with self.assertRaises(ValidationError):
            services.create_policy(self.root, draft, reward_amount=50)

    def test_new_version_retires_previous_and_history_is_immutable(self):
        v1 = self.approved_policy(amount=100)
        v2 = services.approve_policy(self.approver, services.create_policy(self.root, self.campaign, reward_amount=60))
        v1.refresh_from_db()
        self.assertEqual((v1.status, v2.version, v2.status), (P.RETIRED, 2, P.APPROVED))
        self.assertEqual(v1.effective_until, v2.effective_from)
        for change in ({"reward_amount": 1}, {"status": P.DRAFT}, {"campaign_revision": 9}, {"version": 7}):
            with self.subTest(change=change), self.assertRaises(DatabaseError), transaction.atomic():
                RewardPolicy.objects.filter(pk=v1.pk).update(**change)
        with self.assertRaises(DatabaseError), transaction.atomic():
            RewardPolicy.objects.filter(pk=v1.pk).delete()
        with self.assertRaises(IntegrityError), transaction.atomic():  # Never two approved policies.
            RewardPolicy.objects.filter(pk=v1.pk).update(status=P.APPROVED)

    def test_stale_and_rejection(self):
        policy = services.create_policy(self.root, self.campaign, reward_amount=50)
        with self.assertRaises(ValidationError):
            services.reject_policy(self.approver, policy, reason="")
        services.reject_policy(self.approver, policy, reason="Too generous")
        with self.assertRaises(services.StaleDecision):
            services.approve_policy(self.approver, policy, expected_status=P.DRAFT)
        draft = services.create_policy(self.root, self.campaign, reward_amount=40)
        with self.assertRaises(IntegrityError), transaction.atomic():  # Four-eyes in the database too.
            RewardPolicy.objects.filter(pk=draft.pk).update(status=P.APPROVED, decided_by=self.root,
                                                            decided_at=draft.created_at,
                                                            effective_from=draft.created_at)

    def test_campaign_revision_change_blocks_old_draft(self):
        draft = services.create_policy(self.root, self.campaign, reward_amount=50)
        campaign = advertising.pause_campaign(self.owner, self.campaign)
        campaign = advertising.reopen_campaign(self.owner, campaign)
        campaign = advertising.submit_campaign(self.owner, campaign)
        advertising.approve_campaign(self.root, campaign, expected_revision=campaign.revision)
        with self.assertRaises(services.InvalidState) as caught:
            services.approve_policy(self.approver, draft)
        self.assertEqual(caught.exception.code, "campaign_revision_changed")
