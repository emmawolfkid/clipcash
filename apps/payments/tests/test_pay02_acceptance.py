"""PAY-02: end-to-end acceptance of production registration, manual Airtel Lipa membership
payments, staff verification and the membership lifecycle, through the real pages.

Everything here is fictional test data on the isolated PostgreSQL test database. The
"receipt verification" is explicitly simulated by the test (the reviewer ticks the box);
no Airtel system is contacted and no transaction here is a real receipt."""

from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import override_settings
from django.urls import reverse

from apps.accounts.tests.helpers import NEW_PASSWORD, User, make_user
from apps.audit.models import AuditEvent
from apps.memberships import services as memberships
from apps.memberships.models import MembershipPlan, Subscription
from apps.notifications.models import Notification
from apps.payments import manual
from apps.payments.models import ManualMembershipPayment, PaymentIntent, SandboxPosting
from apps.videos import services as videos
from apps.videos.tests.helpers import BUNNY, enable_videos, guid
from apps.wallets import services as ledger
from apps.wallets.exceptions import PostingDisabledError
from apps.wallets.models import JournalEntry, JournalLine
from apps.wallets.posting_types import POSTING_TYPES

from .manual_helpers import ManualPaymentTestCase, staff_with

FICTIONAL_NUMBER = "000111"  # Fictional Lipa number for tests; never a real recipient.
FICTIONAL_RECIPIENT = "TEST ONLY - NOT A REAL RECIPIENT"
FICTIONAL_REFERENCE = "TESTREF0001"


@BUNNY
class EndToEndAcceptanceTests(ManualPaymentTestCase):
    """One member's full journey, driven through the web pages."""

    def setUp(self):
        super().setUp()
        enable_videos()
        self.manager = make_user("content@example.com", role=User.Role.CONTENT_MANAGER)
        category = videos.create_category(self.manager, name="Music", description="Songs")
        video = videos.create_video(self.manager, title="Members clip", category=category)
        from apps.videos.models import Video

        Video.objects.filter(pk=video.pk).update(provider_video_id=guid(), provider_library_id="12345",
                                                 processing_status=Video.Processing.READY, duration_seconds=95)
        video.refresh_from_db()
        self.video = videos.publish(self.manager, video)
        self.plain_staff = staff_with("plain-staff@example.com")
        self.viewer = staff_with("viewer@example.com", "view_manual_payments")

    def playback(self, user):
        return videos.authorize_playback(user, self.video)[0]

    def test_full_journey(self):
        # --- Super Administrator: fictional Lipa details and a plan, through the console ---------
        self.client.force_login(self.root)
        self.client.post(reverse("administration:payment_instructions"),
                         {"business_number": FICTIONAL_NUMBER, "recipient_name": FICTIONAL_RECIPIENT})
        self.assertEqual(manual.active_instruction().business_number, FICTIONAL_NUMBER)
        self.client.post(reverse("administration:plan_create"), {
            "code": "acceptance", "name": "Acceptance plan", "price_amount": "25000", "duration_days": "45",
            "display_order": "1", "is_public": "on", "entitlement_codes": ["videos.standard"]})
        plan = MembershipPlan.objects.get(code="acceptance")
        self.assertEqual((plan.price_amount, plan.duration_days), (25_000, 45))
        self.client.logout()

        # --- Registration, activation, login, logout, password reset -----------------------------
        # REG-02: phone-first sign-up (active at once); email recovery is covered by
        # apps.accounts.tests.test_reg02_phone_first.
        mail.outbox.clear()
        self.assertRedirects(self.client.post(reverse("accounts:register"), {
            "phone_number": "0712 345 678", "password1": NEW_PASSWORD, "password2": NEW_PASSWORD}),
            reverse("dashboard"))
        member = User.objects.get(phone_number="+255712345678")
        self.assertEqual((member.status, member.email), (User.Status.ACTIVE, None))
        login = {"username": "+255712345678", "password": NEW_PASSWORD}
        self.client.post(reverse("accounts:logout"))
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)  # Signed out.
        self.assertEqual(self.client.post(reverse("accounts:login"), login).status_code, 302)

        # --- Unpaid member: no paid content -------------------------------------------------------
        self.assertEqual(self.playback(member), "no_membership")
        detail = self.client.get(reverse("memberships:plan_detail", args=["acceptance"]))
        self.assertContains(detail, "TZS 25,000")
        self.assertContains(detail, "45 days")

        # --- Choose, pay (fictional), submit ------------------------------------------------------
        response = self.client.post(reverse("memberships:choose", args=["acceptance"]))
        pending = Subscription.objects.get(member=member, status="PENDING")
        pay_url = reverse("payments:manual_pay", args=[pending.pk])
        self.assertRedirects(response, pay_url)
        page = self.client.get(pay_url)
        self.assertContains(page, FICTIONAL_NUMBER)
        self.assertContains(page, FICTIONAL_RECIPIENT)
        wrong = self.client.post(pay_url, {"amount": "20000", "payer_phone": "0781234567",
                                           "transaction_reference": FICTIONAL_REFERENCE})
        self.assertContains(wrong, "The amount must be exactly TZS 25,000", status_code=400)
        self.client.post(pay_url, {"amount": "25000", "payer_phone": "0781234567",
                                   "transaction_reference": FICTIONAL_REFERENCE})
        first = ManualMembershipPayment.objects.get(member=member)
        self.assertEqual((first.status, first.amount), ("PENDING_VERIFICATION", 25_000))

        # Nothing is activated by a claim.
        self.assertEqual(Subscription.objects.get(pk=pending.pk).status, "PENDING")
        self.assertEqual(self.playback(member), "no_membership")
        self.assertFalse(JournalEntry.objects.exists())

        # Another member cannot reuse the same reference.
        other = make_user("other@example.com")
        with self.assertRaises(Exception):
            manual.submit_claim(other, memberships.choose_plan(other, plan), amount=25_000,
                                payer_phone="0781234568", transaction_reference=FICTIONAL_REFERENCE)

        # --- Unauthorized staff and the member cannot decide -------------------------------------
        review = reverse("administration:membership_payment", args=[first.pk])
        approve = {"action": "approve", "verified_amount": "25000", "verified_reference": FICTIONAL_REFERENCE,
                   "receipt_confirmed": "on", "recipient_confirmed": "on"}
        for user, expected in ((self.plain_staff, 403), (self.viewer, 403), (member, 302)):
            self.client.force_login(user)
            self.assertEqual(self.client.post(review, approve).status_code, expected, user.email)
        first.refresh_from_db()
        self.assertEqual(first.status, "PENDING_VERIFICATION")

        # --- Reject, then the member resubmits -----------------------------------------------------
        self.client.force_login(self.verifier)
        self.client.post(review, {"action": "reject", "reason": "NOT_FOUND", "note": "Not on the statement yet."})
        first.refresh_from_db()
        self.assertEqual(first.status, "REJECTED")
        self.client.force_login(member)
        self.client.post(pay_url, {"amount": "25000", "payer_phone": "0781234567",
                                   "transaction_reference": FICTIONAL_REFERENCE})
        second = ManualMembershipPayment.objects.get(member=member, status="PENDING_VERIFICATION")

        # --- Approval with explicitly SIMULATED receipt verification (test only) -------------------
        self.client.force_login(self.verifier)
        review = reverse("administration:membership_payment", args=[second.pk])
        self.assertEqual(self.client.post(review, {**approve, "receipt_confirmed": ""}).status_code, 400)
        self.assertRedirects(self.client.post(review, approve), review)
        self.client.post(review, approve)  # Repeated approval: no effect.
        second.refresh_from_db()
        self.assertEqual(second.status, "APPROVED")

        subscription = Subscription.objects.get(pk=pending.pk)
        self.assertEqual((subscription.status, subscription.activation_source, subscription.amount_paid),
                         ("ACTIVE", "PAYMENT", 25_000))
        self.assertEqual(subscription.expires_at - subscription.starts_at, timedelta(days=45))
        self.assertEqual(Subscription.objects.filter(member=member, status="ACTIVE").count(), 1)

        # --- Ledger: one balanced entry, correct accounts -----------------------------------------
        entry = JournalEntry.objects.get()
        lines = list(JournalLine.objects.filter(entry=entry).select_related("account"))
        self.assertEqual(sum(l.amount for l in lines if l.side == "DEBIT"),
                         sum(l.amount for l in lines if l.side == "CREDIT"))
        self.assertEqual({(l.account.account_type, l.side, l.amount) for l in lines},
                         {("CASH", "DEBIT", 25_000), ("MEMBERSHIP_DEFERRED_REVENUE", "CREDIT", 25_000)})
        self.assertEqual(entry.external_reference, FICTIONAL_REFERENCE)
        self.assertEqual(ledger.get_platform_account("CASH").balance, 25_000)
        self.assertFalse(PaymentIntent.objects.exists() or SandboxPosting.objects.exists())

        # --- Notifications and audit trail ---------------------------------------------------------
        kinds = list(Notification.objects.filter(recipient=member).values_list("kind", flat=True))
        for kind in ("membership.payment_submitted", "membership.payment_rejected",
                     "membership.payment_approved", "membership.activated"):
            self.assertIn(kind, kinds)
        actions = set(AuditEvent.objects.values_list("action", flat=True))
        for action in ("account.registered", "membership.payment_instructions."
                       "published", "membership.plan.created", "membership.subscription.created",
                       "membership.manual_payment.submitted", "membership.manual_payment.rejected",
                       "membership.manual_payment.approved", "membership.subscription.activated",
                       "ledger.entry.posted", "membership.manual_payment.refused"):
            self.assertIn(action, actions)
        self.assertEqual(AuditEvent.objects.filter(action="membership.manual_payment.approved").count(), 1)

        # --- Paid content now plays ------------------------------------------------------------------
        self.assertEqual(self.playback(member), "ok")
        self.client.force_login(member)
        self.assertContains(self.client.get(reverse("memberships:mine")), "Acceptance plan")

    def test_lifecycle_expiry_and_cancellation(self):
        claim = self.submit()
        self.approve(claim)
        subscription = Subscription.objects.get(pk=claim.subscription_id)
        self.assertEqual(self.playback(self.member), "ok")
        # Expiry is enforced at the moment it passes, before any job runs.
        with mock.patch("apps.memberships.access.timezone.now", return_value=subscription.expires_at):
            self.assertEqual(self.playback(self.member), "no_membership")
        # Cancellation (e.g. after a manual refund) removes access immediately.
        memberships.cancel(self.root, subscription, Subscription.Reason.REFUND)
        self.assertEqual(self.playback(self.member), "no_membership")
        self.assertEqual(Subscription.objects.get(pk=subscription.pk).cancellation_reason, "REFUND")


class FinancialLockAcceptanceTests(ManualPaymentTestCase):
    def test_only_the_manual_membership_posting_is_open(self):
        cash = ledger.get_platform_account("CASH")
        clearing = ledger.get_platform_account("PAYMENT_CLEARING")
        for code in POSTING_TYPES:
            if code == "membership.manual_payment_received":
                continue
            with self.subTest(code=code), self.assertRaises(PostingDisabledError):
                ledger.post_entry(code, lines=[ledger.debit(cash, 1), ledger.credit(clearing, 1)],
                                  idempotency_key=f"lock-{code}", source_type="test", description="x")
        self.assertFalse(JournalEntry.objects.exists())

    def test_manual_payments_never_run_with_sandbox_or_demo(self):
        for overrides in ({"PAYMENTS_MODE": "sandbox", "PAYMENTS_SANDBOX_ALLOWED": True},
                          {"RESTRICTED_DEMO_MODE": True}):
            with self.subTest(**overrides), override_settings(**overrides):
                self.assertFalse(manual.manual_payments_enabled())
                with self.assertRaises(manual.Unavailable):
                    self.submit()
