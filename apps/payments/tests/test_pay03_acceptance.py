"""
PAY-03: manual Airtel Lipa membership payments, acceptance against the brief.

select plan -> see the company Lipa details -> submit reference -> awaiting verification
-> staff match reference, amount and recipient -> approve once -> active plan, expiry,
included content; rejection with a visible reason; amount mismatch; duplicate
references; repeated and concurrent approval (see test_manual_concurrency); members
approving their own payment or viewing another member's submission.
All records are test records; nothing contacts Airtel.
"""

from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.helpers import make_user
from apps.audit.models import AuditEvent
from apps.memberships.access import check_access
from apps.memberships.models import Subscription
from apps.notifications.models import Notification
from apps.payments import manual
from apps.payments.models import ManualMembershipPayment
from apps.videos import services as videos
from apps.videos.models import Video
from apps.videos.tests.helpers import BUNNY, enable_videos, guid
from apps.wallets.models import JournalEntry

from .manual_helpers import BUSINESS_NUMBER, RECIPIENT, ManualPaymentTestCase, User, airtel_reference

C = ManualMembershipPayment.Status


def pay_url(subscription):
    return reverse("payments:manual_pay", args=[subscription.pk])


def claim_url(claim):
    return reverse("payments:manual_claim", args=[claim.pk])


def review_url(claim):
    return reverse("administration:membership_payment", args=[claim.pk])


QUEUE = reverse("administration:membership_payments")


@BUNNY
class Pay03AcceptanceTests(ManualPaymentTestCase):
    def setUp(self):
        super().setUp()
        enable_videos()
        manager = make_user("content@example.com", role=User.Role.CONTENT_MANAGER)
        category = videos.create_category(manager, name="Music", description="Songs")
        video = videos.create_video(manager, title="Members clip", category=category)
        Video.objects.filter(pk=video.pk).update(provider_video_id=guid(), provider_library_id="12345",
                                                 processing_status=Video.Processing.READY, duration_seconds=95)
        video.refresh_from_db()
        self.video = videos.publish(manager, video)

    def member_submits(self, reference="MP55667788", amount="5000"):
        self.client.force_login(self.member)
        self.client.post(reverse("memberships:choose", args=["monthly"]))
        pending = Subscription.objects.get(member=self.member, status=Subscription.Status.PENDING)
        response = self.client.post(pay_url(pending), {"amount": amount, "payer_phone": "0781234567",
                                                       "transaction_reference": reference})
        return pending, response

    def staff_approve(self, claim, client=None, **overrides):
        client = client or self.client
        data = {"action": "approve", "verified_amount": str(claim.amount),
                "verified_reference": claim.transaction_reference, "receipt_confirmed": "on",
                "recipient_confirmed": "on", **overrides}
        return client.post(review_url(claim), {k: v for k, v in data.items() if v is not None})

    # 1. Member payment ------------------------------------------------------------------

    def test_pay_page_shows_plan_price_duration_and_company_lipa_details(self):
        self.client.force_login(self.member)
        self.client.post(reverse("memberships:choose", args=["monthly"]))
        pending = Subscription.objects.get(member=self.member, status=Subscription.Status.PENDING)
        page = self.client.get(pay_url(pending))
        for text in ("Monthly", "TZS 5,000", "30 days", BUSINESS_NUMBER, RECIPIENT, "Dial *150*60#."):
            self.assertContains(page, text)
        self.assertContains(page, 'name="transaction_reference"')

    def test_submission_awaits_verification_and_keeps_plan_and_quoted_price(self):
        pending, response = self.member_submits()
        claim = ManualMembershipPayment.objects.get()
        self.assertRedirects(response, claim_url(claim))
        page = self.client.get(claim_url(claim))
        self.assertContains(page, "Awaiting payment verification")
        self.assertContains(page, "Submitting a reference does not activate it by itself")
        self.assertContains(page, "You get a notification")
        self.assertEqual((claim.subscription_id, claim.plan_name, claim.amount), (pending.pk, "Monthly", 5_000))
        self.assertEqual((pending.plan_id, pending.price_amount), (self.plan.pk, 5_000))
        self.assertEqual(claim.instruction.business_number, BUSINESS_NUMBER)  # The recipient the member saw.
        # A later price change does not alter what this payment is for.
        from apps.memberships import services as memberships

        memberships.update_plan(self.root, self.plan, {"price_amount": 9_000})
        claim.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual((claim.amount, pending.price_amount), (5_000, 5_000))
        # Pending: no access through this payment.
        self.assertEqual(check_access(self.member, "videos.standard").reason, "no_membership")
        self.assertEqual(videos.authorize_playback(self.member, self.video)[0], "no_membership")

    def test_missing_details_or_disabled_payments_block_submission(self):
        self.client.force_login(self.member)
        self.client.post(reverse("memberships:choose", args=["monthly"]))
        pending = Subscription.objects.get(member=self.member, status=Subscription.Status.PENDING)
        data = {"amount": "5000", "payer_phone": "0781234567", "transaction_reference": "MP11112222"}
        manual.retire_instruction(self.root)
        page = self.client.get(pay_url(pending))
        self.assertContains(page, "has not published its Airtel Lipa payment details", status_code=503)
        self.assertEqual(self.client.post(pay_url(pending), data).status_code, 503)
        manual.publish_instruction(self.root, business_number=BUSINESS_NUMBER, recipient_name=RECIPIENT)
        with override_settings(MANUAL_MEMBERSHIP_PAYMENTS_ENABLED=False):
            page = self.client.get(pay_url(pending))
            self.assertContains(page, "Membership payments are not available at the moment", status_code=503)
            self.assertEqual(self.client.post(pay_url(pending), data).status_code, 503)
        self.assertFalse(ManualMembershipPayment.objects.exists())

    # 2. Staff verification -------------------------------------------------------------

    def test_queue_shows_member_plan_expected_amount_and_reference(self):
        self.member_submits("MP99887766")
        self.client.force_login(self.verifier)
        queue = self.client.get(QUEUE)
        for text in ("member@example.com", "Monthly", "TZS 5,000", "MP99887766"):
            self.assertContains(queue, text)

    def test_reference_alone_never_activates(self):
        self.member_submits()
        claim = ManualMembershipPayment.objects.get()
        self.client.force_login(self.verifier)
        cases = {
            "recipient not confirmed": {"recipient_confirmed": None},
            "receipt not confirmed": {"receipt_confirmed": None},
            "amount mismatch": {"verified_amount": "4000"},
            "reference mismatch": {"verified_reference": "MP00000001"},
        }
        for name, overrides in cases.items():
            with self.subTest(name):
                self.assertEqual(self.staff_approve(claim, **overrides).status_code, 400)
        with self.assertRaisesMessage(ValidationError, "received into the Lipa account"):
            manual.approve(self.verifier, claim, verified_amount=5_000, verified_reference=claim.transaction_reference,
                           receipt_confirmed=True)  # Recipient confirmation missing.
        claim.refresh_from_db()
        self.assertEqual(claim.status, C.PENDING_VERIFICATION)
        self.assertFalse(Subscription.objects.filter(member=self.member, status=Subscription.Status.ACTIVE).exists())
        self.assertFalse(JournalEntry.objects.filter(source_id=str(claim.pk)).exists())
        detail = self.client.get(review_url(claim))
        self.assertContains(detail, f"Lipa {BUSINESS_NUMBER} ({RECIPIENT})")

    def test_approval_activates_exact_plan_once_and_records_the_review(self):
        pending, _ = self.member_submits("MP12121212")
        claim = ManualMembershipPayment.objects.get()
        self.client.force_login(self.verifier)
        before = timezone.now()
        self.assertRedirects(self.staff_approve(claim), review_url(claim))
        self.assertEqual(self.staff_approve(claim).status_code, 302)  # Retry: nothing more happens.
        self.assertEqual(manual.approve(self.verifier, claim, verified_amount=5_000,
                                        verified_reference="MP12121212", receipt_confirmed=True,
                                        recipient_confirmed=True)[1], False)
        claim.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual((claim.status, claim.reviewed_by, claim.verified_reference_key), (C.APPROVED, self.verifier,
                                                                                           "MP12121212"))
        self.assertGreaterEqual(claim.reviewed_at, before)
        self.assertEqual((pending.status, pending.plan_id, pending.amount_paid), (Subscription.Status.ACTIVE,
                                                                                 self.plan.pk, 5_000))
        self.assertEqual(pending.payment_reference, "AIRTEL-LIPA:MP12121212")
        self.assertEqual(Subscription.objects.filter(member=self.member, status=Subscription.Status.ACTIVE).count(), 1)
        self.assertEqual(JournalEntry.objects.filter(source_id=str(claim.pk)).count(), 1)
        event = AuditEvent.objects.get(action="membership.manual_payment.approved")
        self.assertEqual((event.actor_id, event.metadata["recipient"], event.metadata["transaction_reference_key"]),
                         (self.verifier.pk, BUSINESS_NUMBER, "MP12121212"))

    def test_rejection_reason_reaches_the_member_and_grants_nothing(self):
        self.member_submits("MP34343434")
        claim = ManualMembershipPayment.objects.get()
        self.client.force_login(self.verifier)
        self.client.post(review_url(claim), {"action": "reject", "reason": "AMOUNT_MISMATCH",
                                             "note": "We received TZS 4,000."})
        claim.refresh_from_db()
        self.assertEqual(claim.status, C.REJECTED)
        note = Notification.objects.get(recipient=self.member, kind="membership.payment_rejected")
        self.assertIn("We received TZS 4,000.", note.message)
        self.client.force_login(self.member)
        page = self.client.get(claim_url(claim))
        self.assertContains(page, claim.get_rejection_reason_display())
        self.assertContains(page, "We received TZS 4,000.")
        self.assertEqual(check_access(self.member, "videos.standard").reason, "no_membership")

    def test_duplicate_reference_cannot_serve_another_member(self):
        self.member_submits("MP77777777")
        first = ManualMembershipPayment.objects.get()
        self.client.force_login(self.verifier)
        self.staff_approve(first)
        other = make_user("other@example.com")
        self.client.force_login(other)
        self.client.post(reverse("memberships:choose", args=["monthly"]))
        pending = Subscription.objects.get(member=other, status=Subscription.Status.PENDING)
        response = self.client.post(pay_url(pending), {"amount": "5000", "payer_phone": "0789999999",
                                                       "transaction_reference": "mp-7777 7777"})
        self.assertContains(response, "already been submitted", status_code=400)
        self.assertEqual(ManualMembershipPayment.objects.filter(member=other).count(), 0)
        self.assertEqual(check_access(other, "videos.standard").reason, "no_membership")

    def test_members_cannot_approve_or_view_other_submissions(self):
        self.member_submits("MP45454545")
        claim = ManualMembershipPayment.objects.get()
        other = make_user("other@example.com")
        self.client.force_login(other)
        self.assertEqual(self.client.get(claim_url(claim)).status_code, 404)
        self.assertEqual(self.client.post(claim_url(claim), {"action": "cancel"}).status_code, 404)
        self.client.force_login(self.member)  # The owner, as a plain member, cannot reach the console.
        for response in (self.client.get(review_url(claim)), self.staff_approve(claim)):
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response["Location"].startswith(reverse("admin:login")))
        # A staff verifier cannot approve their own payment either.
        self.verifier.user_permissions.add(Permission.objects.get(codename="verify_manual_payments"))
        own = self.submit(member=self.verifier, reference=airtel_reference())
        self.client.force_login(self.verifier)
        self.assertEqual(self.staff_approve(own).status_code, 403)
        own.refresh_from_db()
        claim.refresh_from_db()
        self.assertEqual((own.status, claim.status), (C.PENDING_VERIFICATION, C.PENDING_VERIFICATION))

    # 3. Member outcome -----------------------------------------------------------------

    def test_member_sees_active_plan_expiry_content_and_notification(self):
        self.member_submits("MP56565656")
        claim = ManualMembershipPayment.objects.get()
        self.client.force_login(self.verifier)
        self.staff_approve(claim)
        subscription = Subscription.objects.get(member=self.member, status=Subscription.Status.ACTIVE)
        note = Notification.objects.get(recipient=self.member, kind="membership.payment_approved")
        self.assertIn("Monthly membership is active until", note.message)
        self.client.force_login(self.member)
        page = self.client.get(claim_url(claim))
        self.assertContains(page, "Payment confirmed: your membership is active")
        self.assertContains(page, "Standard video library")
        self.assertContains(page, f'href="{reverse("videos:library")}"')
        mine = self.client.get(reverse("memberships:mine"))
        self.assertContains(mine, "Monthly")
        self.assertContains(mine, "Open included videos")
        self.assertIsNotNone(subscription.expires_at)
        self.assertEqual(videos.authorize_playback(self.member, self.video)[0], "ok")
        self.assertEqual(self.client.get(reverse("videos:detail", args=[self.video.slug])).status_code, 200)
