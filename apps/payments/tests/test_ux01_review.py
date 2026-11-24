"""UX-01: the simplified membership payment review.

The console asks for one typed value (the provider's transaction ID) and three confirmations;
everything else ClipCash already knows. These tests pin the financial safeguards that must
survive the simpler form (verification, single use, locking/idempotency, authorization, audit),
the immediate activation, and the path from a confirmed payment to watching an included video.
"""

from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.helpers import User, make_user
from apps.audit.models import AuditEvent
from apps.memberships import services as memberships
from apps.memberships.access import check_access
from apps.memberships.models import Subscription
from apps.notifications.models import Notification
from apps.payments import manual
from apps.payments.models import ManualMembershipPayment
from apps.videos import services as videos
from apps.videos.models import Video
from apps.videos.tests.helpers import BUNNY, enable_videos, guid
from apps.wallets.models import JournalEntry

from .manual_helpers import BUSINESS_NUMBER, ManualPaymentTestCase, staff_with

C = ManualMembershipPayment.Status
QUEUE = reverse("administration:membership_payments")
CONFIRM_BUTTON = "Confirm Payment &amp; Activate Membership"


def review_url(claim):
    return reverse("administration:membership_payment", args=[claim.pk])


def confirm(reference, **overrides):
    """The complete console approval: one typed value and three ticks."""
    data = {"action": "approve", "verified_reference": reference, "receipt_confirmed": "on",
            "amount_confirmed": "on", "recipient_confirmed": "on"}
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


class ReviewPageTests(ManualPaymentTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.verifier)

    def test_queue_filters_by_status_with_counts_and_a_review_action(self):
        pending = self.submit(reference="MP10101010")
        approved = self.submit(member=make_user("approved@example.com"), reference="MP20202020")
        self.approve(approved)
        rejected = self.submit(member=make_user("rejected@example.com"), reference="MP30303030")
        manual.reject(self.verifier, rejected, reason="NOT_FOUND")

        queue = self.client.get(QUEUE)
        self.assertContains(queue, "css/pages/administration/membership-payments.css")
        self.assertNotContains(queue, "style=")
        for label in ("Pending", "Approved", "Rejected"):
            self.assertContains(queue, f'{label} <span class="payment-queue__count">1</span>')
        self.assertContains(queue, review_url(pending))
        self.assertContains(queue, ">Review<")
        self.assertNotContains(queue, review_url(approved))
        self.assertNotContains(queue, review_url(rejected))

        for status, shown in ((C.APPROVED, approved), (C.REJECTED, rejected)):
            page = self.client.get(QUEUE, {"status": status})
            self.assertContains(page, review_url(shown))
            self.assertNotContains(page, review_url(pending))
            self.assertContains(page, ">View<")
        # Search stays inside the chosen tab.
        self.assertContains(self.client.get(QUEUE, {"status": C.APPROVED, "q": "MP2020"}), review_url(approved))
        self.assertNotContains(self.client.get(QUEUE, {"q": "MP2020"}), review_url(approved))

    def test_detail_is_compact_with_secondary_details_collapsed(self):
        claim = self.submit(reference="MP40404040")
        page = self.client.get(review_url(claim))
        content = page.content.decode()
        self.assertContains(page, "css/pages/administration/membership-payments.css")
        self.assertContains(page, "js/payment-review.js")
        self.assertNotContains(page, "style=")
        self.assertContains(page, CONFIRM_BUTTON, count=1)
        # One typed field; the amount and the receiving account are confirmed, never retyped.
        self.assertContains(page, 'name="verified_reference"')
        for name in ("receipt_confirmed", "amount_confirmed", "recipient_confirmed"):
            self.assertContains(page, f'name="{name}"')
        for retired in ("verified_amount", "verified_receiving_number", "verified_recipient_name"):
            self.assertNotContains(page, f'name="{retired}"')
        self.assertContains(page, "The amount received is exactly TZS 5,000.")
        self.assertContains(page, f"Lipa {BUSINESS_NUMBER}")
        # The internal claim reference and audit trail live in collapsed sections, after the action.
        self.assertLess(content.index(CONFIRM_BUTTON), content.index("Technical details"))
        self.assertIn('<details class="payment-review__section">\n    <summary>Technical details', content)
        self.assertIn("Audit history (1 event)", content)
        primary = content[content.index("payment-review__back"):content.index("Technical details")]
        self.assertNotIn(claim.reference, primary)  # Nobody needs to read or copy the internal reference.
        # Rejection is secondary: collapsed until it is needed.
        self.assertIn('<details class="payment-review__reject">', content)
        self.assertContains(page, "Activation unlocks: Standard video library")

    def test_rejection_form_opens_again_on_errors(self):
        claim = self.submit()
        response = self.client.post(review_url(claim), {"action": "reject", "reason": "OTHER", "note": ""})
        self.assertContains(response, '<details class="payment-review__reject" open>', status_code=400)
        self.assertContains(response, "Explain the reason", status_code=400)

    def test_plan_without_video_access_is_flagged(self):
        plan = self.make_plan("audio", entitlements=[])
        claim = self.submit(subscription=self.choose(plan=plan))
        page = self.client.get(review_url(claim))
        self.assertContains(page, "This plan includes no video access")
        self.assertContains(page, "This plan unlocks nothing.")


class ApprovalTests(ManualPaymentTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.verifier)

    def test_confirming_activates_the_membership_once_and_records_everything(self):
        claim = self.submit(reference="MP50505050")
        before = timezone.now()
        response = self.client.post(review_url(claim), confirm("mp5050 5050", note="Seen on statement"),
                                    follow=True)
        self.assertRedirects(response, review_url(claim))
        self.assertContains(response, "Payment confirmed. Monthly membership is active now")
        self.assertContains(response, "Payment confirmed: membership active")

        claim.refresh_from_db()
        subscription = Subscription.objects.get(pk=claim.subscription_id)
        self.assertEqual((claim.status, claim.reviewed_by, claim.verified_amount, claim.verified_reference_key,
                          claim.receipt_confirmed, claim.review_note),
                         (C.APPROVED, self.verifier, 5_000, "MP50505050", True, "Seen on statement"))
        self.assertEqual(subscription.status, Subscription.Status.ACTIVE)
        self.assertLessEqual(subscription.starts_at, timezone.now())
        self.assertGreaterEqual(subscription.activated_at, before)
        self.assertEqual(subscription.payment_reference, "AIRTEL-LIPA:MP50505050")
        entry = JournalEntry.objects.get()
        self.assertEqual((entry.total_amount, entry.external_reference, entry.source_id),
                         (5_000, "MP50505050", str(claim.pk)))
        event = AuditEvent.objects.get(action="membership.manual_payment.approved")
        self.assertEqual((event.metadata["amount_source"], event.metadata["reference_source"],
                          event.metadata["recipient"]), ("confirmed", "member", BUSINESS_NUMBER))
        self.assertTrue(Notification.objects.filter(recipient=self.member,
                                                    kind="membership.payment_approved").exists())
        # Entitlements are available at once: no second administrator action.
        self.assertTrue(check_access(self.member, entitlement="videos.standard"))

        # Repeated submissions (double click, back button, retry) change nothing.
        again = self.client.post(review_url(claim), confirm("MP50505050"), follow=True)
        self.assertContains(again, "This payment was already confirmed. Nothing changed.")
        self.assertEqual(JournalEntry.objects.count(), 1)
        self.assertEqual(Subscription.objects.filter(member=self.member, status=Subscription.Status.ACTIVE).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="membership.manual_payment.approved").count(), 1)

    def test_every_confirmation_and_the_transaction_id_are_required(self):
        claim = self.submit(reference="MP60606060")
        cases = {
            "no transaction ID": {"verified_reference": ""},
            "not found in provider records": {"receipt_confirmed": None},
            "amount not confirmed": {"amount_confirmed": None},
            "receiving account not confirmed": {"recipient_confirmed": None},
            "different transaction ID": {"verified_reference": "MP60606061"},
            "invalid transaction ID": {"verified_reference": "x"},
        }
        for name, overrides in cases.items():
            with self.subTest(name):
                response = self.client.post(review_url(claim), confirm("MP60606060", **overrides))
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, "error-summary", status_code=400)
        claim.refresh_from_db()
        self.assertEqual(claim.status, C.PENDING_VERIFICATION)
        self.assertFalse(JournalEntry.objects.exists())
        self.assertFalse(Subscription.objects.filter(member=self.member, status=Subscription.Status.ACTIVE).exists())

    def test_service_still_refuses_unverified_or_mismatched_approvals(self):
        claim = self.submit(reference="MP70707070")
        base = dict(verified_reference="MP70707070", receipt_confirmed=True, recipient_confirmed=True)
        with self.assertRaises(ValidationError) as caught:
            manual.approve(self.verifier, claim, **base)  # Amount neither confirmed nor typed.
        self.assertIn("amount_confirmed", caught.exception.message_dict)
        with self.assertRaises(ValidationError) as caught:
            manual.approve(self.verifier, claim, verified_amount=4_000, **base)
        self.assertIn("verified_amount", caught.exception.message_dict)
        with self.assertRaises(ValidationError) as caught:
            manual.approve(self.verifier, claim, amount_confirmed=True, verified_receiving_number="999999", **base)
        self.assertIn("verified_receiving_number", caught.exception.message_dict)
        self.assertFalse(JournalEntry.objects.exists())
        # Typed values that match (older callers) still approve, recorded as entered.
        self.assertTrue(manual.approve(self.verifier, claim, verified_amount="5,000",
                                       verified_receiving_number=BUSINESS_NUMBER, **base)[1])
        event = AuditEvent.objects.get(action="membership.manual_payment.approved")
        self.assertEqual(event.metadata["amount_source"], "entered")

    def test_a_provider_transaction_can_back_only_one_membership(self):
        first = self.submit(reference="MP80808080")
        self.client.post(review_url(first), confirm("MP80808080"))
        # The second member gave no reference; the reviewer finds the same transaction.
        other = make_user("other@example.com")
        second, _ = manual.submit_claim(other, self.choose(other), payer_phone="", transaction_reference="")
        response = self.client.post(review_url(second), confirm("MP 8080 8080"))
        self.assertContains(response, "already used by another membership payment", status_code=400)
        second.refresh_from_db()
        self.assertEqual((second.status, second.reference_key), (C.PENDING_VERIFICATION, ""))
        self.assertEqual(JournalEntry.objects.count(), 1)
        refusal = AuditEvent.objects.filter(action="membership.manual_payment.refused").latest("occurred_at")
        self.assertEqual(refusal.metadata, {"action": "approve", "reason": "duplicate_reference"})

    def test_reused_transaction_id_is_flagged_on_the_review_page(self):
        third = make_user("third@example.com")
        earlier, _ = manual.submit_claim(third, self.choose(third), transaction_reference="MP91919191")
        manual.reject(self.verifier, earlier, reason="NOT_FOUND")
        fourth = make_user("fourth@example.com")
        later, _ = manual.submit_claim(fourth, self.choose(fourth), transaction_reference="MP91919191")
        page = self.client.get(review_url(later))
        self.assertContains(page, "This transaction ID is also on other submissions")
        self.assertContains(page, review_url(earlier))
        self.assertNotContains(self.client.get(review_url(self.submit(reference="MP92929292"))),
                               "This transaction ID is also on other submissions")

    def test_rejection_notifies_the_member_and_allows_resubmission(self):
        claim = self.submit()
        response = self.client.post(review_url(claim), {"action": "reject", "reason": "NOT_FOUND"}, follow=True)
        self.assertContains(response, "Payment rejected. The member was notified and can send corrected details.")
        self.assertContains(response, "Payment rejected")
        claim.refresh_from_db()
        self.assertEqual(claim.status, C.REJECTED)
        self.assertTrue(Notification.objects.filter(recipient=self.member, kind="membership.payment_rejected").exists())
        self.assertFalse(JournalEntry.objects.exists())
        self.assertNotContains(self.client.get(review_url(claim)), CONFIRM_BUTTON)
        # A rejected payment can never be approved afterwards.
        with self.assertRaises(manual.InvalidState):
            manual.approve(self.verifier, claim, verified_reference=claim.transaction_reference,
                           receipt_confirmed=True, recipient_confirmed=True, amount_confirmed=True)


class AuthorizationTests(ManualPaymentTestCase):
    def test_only_verifiers_can_confirm_or_reject(self):
        claim = self.submit(reference="MP12341234")
        viewer = staff_with("viewer@example.com", "view_manual_payments")
        plain = staff_with("plain@example.com")
        for user, expected in ((viewer, 403), (plain, 403)):
            self.client.force_login(user)
            for data in (confirm("MP12341234"), {"action": "approve"}, {"action": "reject", "reason": "NOT_FOUND"}):
                with self.subTest(user=user.email, action=data["action"]):
                    self.assertEqual(self.client.post(review_url(claim), data).status_code, expected)
        # The view-only reviewer's attempts are audited, before their input is even validated.
        refusals = AuditEvent.objects.filter(action="membership.manual_payment.refused",
                                             actor_id=viewer.pk)
        self.assertEqual(refusals.count(), 3)
        self.assertEqual(refusals.first().metadata["reason"], "not_authorized")
        # Viewers see the payment but no form.
        self.client.force_login(viewer)
        page = self.client.get(review_url(claim))
        self.assertContains(page, "You can view this payment but not approve or reject it.")
        self.assertNotContains(page, CONFIRM_BUTTON)
        # Members and anonymous visitors are sent to sign in as staff.
        for client in (Client(), self.client):
            if client is self.client:
                client.force_login(self.member)
            response = client.post(review_url(claim), confirm("MP12341234"))
            self.assertEqual(response.status_code, 302)
            self.assertIn(reverse("admin:login"), response["Location"])
        claim.refresh_from_db()
        self.assertEqual(claim.status, C.PENDING_VERIFICATION)
        self.assertFalse(JournalEntry.objects.exists())

    def test_staff_cannot_confirm_their_own_payment(self):
        own = self.submit(member=self.verifier, reference="MP56785678")
        self.client.force_login(self.verifier)
        self.assertNotContains(self.client.get(review_url(own)), CONFIRM_BUTTON)
        self.assertEqual(self.client.post(review_url(own), confirm("MP56785678")).status_code, 403)
        own.refresh_from_db()
        self.assertEqual(own.status, C.PENDING_VERIFICATION)
        self.assertFalse(JournalEntry.objects.exists())

    def test_unknown_actions_are_refused(self):
        claim = self.submit()
        self.client.force_login(self.verifier)
        self.assertEqual(self.client.post(review_url(claim), {"action": "activate"}).status_code, 403)


class MemberStatusTests(ManualPaymentTestCase):
    def test_member_sees_clear_status_after_confirmation(self):
        claim = self.submit()
        self.client.force_login(self.member)
        page = self.client.get(reverse("payments:manual_claim", args=[claim.pk]))
        self.assertContains(page, "Verification requested")
        self.approve(claim)
        page = self.client.get(reverse("payments:manual_claim", args=[claim.pk]))
        self.assertContains(page, "Payment confirmed: your membership is active")
        self.assertContains(page, "Your Monthly membership is active now")
        self.assertContains(page, "Standard video library")

    def test_a_renewal_confirmed_early_says_when_it_starts(self):
        self.approve(self.submit())
        current = Subscription.objects.get(member=self.member, status=Subscription.Status.ACTIVE)
        near_end = current.expires_at - timedelta(days=5)  # Inside the renewal window.
        with mock.patch("apps.memberships.services.timezone.now", return_value=near_end), \
                mock.patch("apps.payments.manual.timezone.now", return_value=near_end):
            claim = self.submit(subscription=memberships.choose_plan(self.member, self.plan))
            self.approve(claim)
        renewal = Subscription.objects.get(pk=claim.subscription_id)
        self.assertEqual(renewal.starts_at, current.expires_at)  # Queued after the current period.
        self.client.force_login(self.member)
        page = self.client.get(reverse("payments:manual_claim", args=[claim.pk]))
        self.assertContains(page, "Payment confirmed: your new membership is booked")
        self.assertContains(page, "You keep your current access until then.")


@BUNNY
class PaymentToVideoAccessTests(ManualPaymentTestCase):
    """The exact path from a confirmed payment to watching: library_queryset (published, ready,
    active category) filtered by the plan's entitlements, and authorize_playback (check_access
    against the video's required entitlement)."""

    def setUp(self):
        super().setUp()
        enable_videos()
        self.manager = make_user("content@example.com", role=User.Role.CONTENT_MANAGER)
        self.category = videos.create_category(self.manager, name="Music", description="Songs")
        self.included = self.published("Included clip")
        self.premium = self.published("Premium clip", required_entitlement="videos.premium")
        self.draft = self.ready(videos.create_video(self.manager, title="Draft clip", category=self.category))
        # Not ready: still processing, so it cannot be published (the database refuses that too).
        self.processing = videos.create_video(self.manager, title="Processing clip", category=self.category)
        self.archived = videos.archive(self.manager, self.published("Archived clip"))
        self.member_client = Client()
        self.member_client.force_login(self.member)

    def ready(self, video):
        Video.objects.filter(pk=video.pk).update(provider_video_id=guid(), provider_library_id="12345",
                                                 processing_status=Video.Processing.READY, duration_seconds=60)
        video.refresh_from_db()
        return video

    def published(self, title, **values):
        video = self.ready(videos.create_video(self.manager, title=title, category=self.category, **values))
        return videos.publish(self.manager, video)

    def detail(self, video):
        return self.member_client.get(reverse("videos:detail", args=[video.slug]))

    def test_confirmed_payment_unlocks_exactly_the_included_ready_published_videos(self):
        claim = self.submit(reference="MP13579135")
        # Before confirmation: no catalogue, no playback.
        self.assertRedirects(self.member_client.get(reverse("videos:library")), reverse("memberships:plans"),
                             fetch_redirect_response=False)
        self.assertEqual(videos.authorize_playback(self.member, self.included)[0], "no_membership")

        self.client.force_login(self.verifier)
        self.assertEqual(self.client.post(review_url(claim), confirm("MP13579135")).status_code, 302)

        library = self.member_client.get(reverse("videos:library"))
        self.assertEqual(library.status_code, 200)
        self.assertContains(library, "Included clip")
        for hidden in ("Premium clip", "Draft clip", "Processing clip", "Archived clip"):
            self.assertNotContains(library, hidden)

        page = self.detail(self.included)
        self.assertEqual((page.status_code, page.context["playback_reason"]), (200, "ok"))
        self.assertIsNotNone(page.context["playback"])

        # Not in the plan: refused with the entitlement reason, no player.
        refused = self.detail(self.premium)
        self.assertRedirects(refused, reverse("memberships:plans"), fetch_redirect_response=False)
        self.assertEqual(videos.authorize_playback(self.member, self.premium)[0], "entitlement_missing")
        # Unpublished, archived or not ready: not found, and never playable.
        for video in (self.draft, self.processing, self.archived):
            with self.subTest(video=video.title):
                self.assertEqual(self.detail(video).status_code, 404)
                self.assertEqual(videos.authorize_playback(self.member, video)[0], "unavailable")
        # Another account gains nothing from this payment.
        stranger = Client()
        stranger.force_login(make_user("stranger@example.com"))
        self.assertEqual(stranger.get(reverse("videos:detail", args=[self.included.slug])).status_code, 302)
