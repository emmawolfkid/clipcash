"""Phase 15-A: notifications produced by the authoritative business services."""

import re
import smtplib
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.helpers import AccountsTestCase, User, run_worker
from apps.advertising import services as advertising
from apps.core.jobs.services import enqueue, process_jobs
from apps.memberships import services as memberships
from apps.memberships.models import Subscription
from apps.memberships.tests.helpers import MembershipTestCase
from apps.memberships.tests.test_complimentary import GRANTS
from apps.notifications import inbox
from apps.notifications.models import Notification, OutboundEmail
from apps.rewards import services as rewards
from apps.rewards.tests.helpers import RewardsTestCase, make_user
from apps.videos.tests.helpers import VideoTestCase
from apps.viewing import services as viewing
from apps.withdrawals import services as withdrawals
from apps.withdrawals.tests.helpers import WithdrawalsTestCase

AMOUNT = re.compile(r"\d{3,}|TZS")  # Notice texts must state no amounts.


def kinds(user):
    return sorted(Notification.objects.filter(recipient=user).values_list("kind", flat=True))


class AccountEventTests(AccountsTestCase):
    def test_password_change_creates_a_security_notice_and_auth_emails_are_unchanged(self):
        from apps.accounts.tests.helpers import NEW_PASSWORD, PASSWORD

        member = make_user("member@example.com")
        self.client.force_login(member)
        self.client.post(reverse("accounts:password_change"), {
            "old_password": PASSWORD, "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
        self.assertEqual(kinds(member), ["account.password_changed"])
        self.assertEqual(len(mail.outbox), 0)  # In-app only: no new email behaviour.

    @override_settings(RESTRICTED_DEMO_MODE=False, PUBLIC_REGISTRATION_APPROVED=True)
    def test_phone_registration_sends_no_email_or_notice(self):
        # REG-02: phone-first sign-up has no address to write to.
        self.client.post(reverse("accounts:register"), {"phone_number": "0712345678",
                                                        "password1": "Another-Strong-Phrase-42",
                                                        "password2": "Another-Strong-Phrase-42"})
        self.assertTrue(User.objects.filter(phone_number="+255712345678").exists())
        self.assertFalse(OutboundEmail.objects.exists())
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(Notification.objects.exists())


@override_settings(**GRANTS, MEMBERSHIPS_EXPIRY_REMINDER_DAYS=7)
class MembershipEventTests(MembershipTestCase):
    def grant(self, days=3):
        return memberships.grant_complimentary(self.root, self.member, self.plan,
                                               expires_at=timezone.now() + timedelta(days=days), reason="Demo grant")

    def test_complimentary_grant_notifies_and_emails_the_member(self):
        self.grant()
        notice = Notification.objects.get(recipient=self.member)
        self.assertEqual((notice.kind, notice.category), ("membership.complimentary", "MEMBERSHIP"))
        self.assertIn("No payment was taken", notice.message)
        self.assertEqual(OutboundEmail.objects.filter(kind=inbox.NOTICE_EMAIL, user_id=self.member.pk).count(), 1)

    def test_expiry_reminder_once_per_membership_and_skipped_with_a_renewal(self):
        subscription = self.grant(days=3)
        self.assertEqual(memberships.send_expiry_reminders(), 1)
        self.assertEqual(memberships.send_expiry_reminders(), 0)  # Retried or repeated job.
        self.assertEqual(Notification.objects.filter(kind="membership.expiring").count(), 1)
        self.assertEqual(Subscription.objects.get(pk=subscription.pk).status, "ACTIVE")  # Nothing renewed.
        self.assertFalse(Subscription.objects.filter(member=self.member, status="PENDING").exists())

        other = make_user("renewing@example.com")
        current = self.active_membership(member=other)
        later = current.expires_at - timedelta(days=5)
        with mock.patch("apps.memberships.services.timezone.now", return_value=later):
            self.activate(memberships.choose_plan(other, self.plan))  # Renewal queued.
            self.assertEqual(memberships.send_expiry_reminders(now=later), 0)
        self.assertFalse(Notification.objects.filter(recipient=other, kind="membership.expiring").exists())

    def test_reminder_window_is_configurable_and_can_be_disabled(self):
        self.grant(days=3)
        with override_settings(MEMBERSHIPS_EXPIRY_REMINDER_DAYS=0):
            self.assertEqual(memberships.send_expiry_reminders(), 0)
        with override_settings(MEMBERSHIPS_EXPIRY_REMINDER_DAYS=2):
            self.assertEqual(memberships.send_expiry_reminders(), 0)  # Ends in 3 days: outside the window.

    def test_reminder_job_retries_safely(self):
        self.grant(days=3)
        for _ in range(2):
            enqueue("memberships.expiry_reminders", {})
            process_jobs("test-worker", max_jobs=10, max_seconds=10)
        self.assertEqual(Notification.objects.filter(kind="membership.expiring").count(), 1)

    def test_expiry_suspension_and_own_cancellation(self):
        subscription = self.grant(days=1)
        memberships.suspend(self.root, subscription, "ADMIN_REVIEW")
        memberships.process_expiry(now=timezone.now() + timedelta(days=2))
        self.assertEqual(kinds(self.member), ["membership.complimentary", "membership.expired",
                                              "membership.suspended"])
        suspended = Notification.objects.get(kind="membership.suspended")
        self.assertNotIn("ADMIN_REVIEW", suspended.message)  # Reasons are never stated.
        other = make_user("chooser@example.com")
        memberships.cancel_pending(other, memberships.choose_plan(other, self.plan))
        self.assertEqual(kinds(other), [])  # The member's own action is not notified.

    def test_email_failure_does_not_undo_the_grant(self):
        subscription = self.grant()
        with mock.patch("apps.notifications.services.send_mail",
                        side_effect=smtplib.SMTPServerDisconnected("down")):
            run_worker()
        self.assertEqual(Subscription.objects.get(pk=subscription.pk).status, "ACTIVE")
        self.assertTrue(Notification.objects.filter(recipient=self.member).exists())
        self.assertEqual(OutboundEmail.objects.get(user_id=self.member.pk).status, "PENDING")  # Will retry.


class AdvertisingEventTests(RewardsTestCase):
    def test_campaign_lifecycle_notifies_the_team_and_reviewers_only(self):
        manager = make_user("manager@brand.example")
        invitation = advertising.invite_member(self.owner, self.org, manager.email)
        self.assertEqual(kinds(manager), ["advertiser.invitation"])
        advertising.accept_invitation(manager, invitation.pk)
        outsider = self.new_member("outsider@example.com")
        campaign = advertising.submit_campaign(self.owner, self.make_campaign(name="Spring"))
        self.assertIn("campaign.review_needed", kinds(self.root))
        advertising.approve_campaign(self.root, campaign, expected_revision=campaign.revision)
        for person in (self.owner, manager):
            self.assertIn("campaign.approved", kinds(person))
            self.assertIn("campaign.submitted", kinds(person))
        self.assertFalse(Notification.objects.filter(recipient=outsider, category="ADVERTISING").exists(),
                         kinds(outsider))  # Other members and organizations see nothing of it.

    def test_staff_pause_notifies_but_the_teams_own_pause_does_not(self):
        advertising.pause_campaign(self.owner, self.campaign, reason="Our pause")
        self.assertNotIn("campaign.paused", kinds(self.owner))
        advertising.resume_campaign(self.owner, self.campaign)
        advertising.pause_campaign(self.root, self.campaign, reason="Policy check")
        self.assertIn("campaign.paused", kinds(self.owner))
        self.assertNotIn("Policy check", Notification.objects.get(kind="campaign.paused").message)

    def test_organization_decisions_notify_the_team_and_applications_the_reviewers(self):
        applicant = self.new_member("applicant@example.com")
        organization = self.make_org(applicant, "Newco")
        self.assertIn("advertiser.review_needed", kinds(self.root))
        advertising.review_organization(self.root, organization, "reject", expected_status="PENDING",
                                        reason="Missing registration")
        notice = Notification.objects.get(recipient=applicant, kind="advertiser.rejected")
        self.assertNotIn("Missing registration", notice.message)


class ViewingAndRewardEventTests(RewardsTestCase):
    def test_reward_and_revocation_notices_are_simulated_and_state_no_amounts(self):
        self.approved_policy(amount=100)
        session = self.verified_session()
        rewards.evaluate_pending()
        viewing.revoke_verified_session(self.root, session, reason="Fraud confirmed")
        self.assertEqual([k for k in kinds(self.member) if k.startswith(("rewards.", "viewing."))],
                         ["rewards.recorded", "rewards.reversed", "viewing.revoked"])
        for notice in Notification.objects.filter(recipient=self.member, category__in=["REWARD", "VIEWING"]):
            self.assertIn("simulated" if notice.category == "REWARD" else "view", notice.title.lower() + notice.message)
            self.assertIsNone(AMOUNT.search(notice.title + notice.message), notice.message)
            self.assertNotIn("Fraud", notice.message)

    def test_policy_draft_asks_the_other_approver_not_the_author(self):
        rewards.create_policy(self.root, self.campaign, reward_amount=100)
        self.assertIn("rewards.policy_review_needed", kinds(self.approver))
        self.assertNotIn("rewards.policy_review_needed", kinds(self.root))


class WithdrawalEventTests(WithdrawalsTestCase):
    def test_demo_withdrawal_notices_say_demo_and_reach_other_reviewers(self):
        demo = self.approved()
        member_kinds = kinds(self.member)
        self.assertIn("withdrawals.demo_requested", member_kinds)
        self.assertIn("withdrawals.demo_approved", member_kinds)
        self.assertIn("withdrawals.review_needed", kinds(self.approver))
        self.assertNotIn("withdrawals.review_needed", kinds(self.root))  # The creator is not asked to review.
        for notice in Notification.objects.filter(recipient=self.member, category="WITHDRAWAL"):
            self.assertRegex(notice.title, "(?i)demo|simulat")
            self.assertIsNone(AMOUNT.search(notice.title + notice.message))
        self.assertEqual(demo.status, "DEMO_APPROVED")

    def test_demo_rejection_notifies_the_member(self):
        demo = self.demo()
        demo = withdrawals.start_review(self.approver, demo, expected_status=demo.status)
        withdrawals.reject(self.approver, demo, expected_status=demo.status,
                           reason_code=withdrawals.REJECTION_REASONS[0], note="Not today")
        notice = Notification.objects.get(recipient=self.member, kind="withdrawals.demo_rejected")
        self.assertNotIn("Not today", notice.message)


class OperationsEventTests(AccountsTestCase):
    def test_terminal_job_failure_alerts_operators_once(self):
        from apps.core.jobs.errors import PermanentJobError
        from apps.core.jobs.registry import JOB_TYPES, register

        from apps.accounts.tests.helpers import User

        root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        member = make_user("member@example.com")

        @register("tests.always_fails", max_attempts=1)
        def always_fails(payload, job):
            raise PermanentJobError("broken")

        self.addCleanup(JOB_TYPES.pop, "tests.always_fails", None)
        job = enqueue("tests.always_fails", {})
        process_jobs("test-worker", max_jobs=10, max_seconds=10)
        process_jobs("test-worker", max_jobs=10, max_seconds=10)
        alerts = Notification.objects.filter(kind="operations.job.failed")
        self.assertEqual([n.recipient for n in alerts], [root])
        self.assertIn("tests.always_fails", alerts.get().message)
        self.assertEqual(kinds(member), [])
        self.assertEqual(alerts.get().dedupe_key, f"job.failed:{job.pk}")



class VideoEventTests(VideoTestCase):
    def test_failed_upload_notifies_the_creator_once(self):
        from apps.videos import services as videos
        from apps.videos.models import Video

        video = self.make_video("Clip")
        Video.objects.filter(pk=video.pk).update(processing_status=Video.Processing.UPLOADING)
        videos.report_upload_failed(self.manager, video, "network")
        videos.report_upload_failed(self.manager, video, "network")  # Repeated report: already failed.
        self.assertEqual(kinds(self.manager), ["video.failed"])
        self.assertEqual(Notification.objects.get(recipient=self.manager).link,
                         reverse("administration:video_detail", args=[video.pk]))
