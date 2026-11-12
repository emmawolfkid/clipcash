"""
REG-02 acceptance: phone-first members end to end, with test records only.

sign up with a mobile number -> dashboard -> choose a plan -> submit an Airtel Lipa
payment -> staff verify and approve -> dashboard and inbox show the decision -> included
video plays. Email copies go only to a verified address; recovery never trusts a phone
number; existing email members and staff keep signing in as before.
"""

from django.contrib.auth import get_user
from django.core import mail
from django.core.cache import cache
from django.urls import reverse

from apps.accounts.access import identity_ready
from apps.accounts.tests.helpers import NEW_PASSWORD, PASSWORD, links_in_last_email, make_user, run_worker
from apps.audit.models import AuditEvent
from apps.memberships.access import check_access
from apps.memberships.models import Subscription
from apps.notifications.models import Notification, OutboundEmail
from apps.payments.models import ManualMembershipPayment
from apps.payments.tests.manual_helpers import BUSINESS_NUMBER, ManualPaymentTestCase, User
from apps.videos import services as videos
from apps.videos.models import Video
from apps.videos.tests.helpers import BUNNY, enable_videos, guid

REGISTER = reverse("accounts:register")
LOGIN = reverse("accounts:login")
DASHBOARD = reverse("dashboard")
RESET = reverse("accounts:password_reset")
INBOX = reverse("notifications:inbox")
QUEUE = reverse("administration:membership_payments")
PHONE = "+255712345678"


@BUNNY
class PhoneFirstJourneyTests(ManualPaymentTestCase):
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
        self.staff = self.client_class()
        self.staff.force_login(self.verifier)

    def sign_up(self, phone="0712 345 678"):
        response = self.client.post(REGISTER, {"phone_number": phone, "password1": NEW_PASSWORD,
                                               "password2": NEW_PASSWORD})
        self.assertRedirects(response, DASHBOARD)
        return User.objects.get(phone_number=PHONE)

    def pay(self, reference="MP24681357"):
        self.client.post(reverse("memberships:choose", args=["monthly"]))
        pending = Subscription.objects.get(member__phone_number=PHONE, status=Subscription.Status.PENDING)
        self.client.post(reverse("payments:manual_pay", args=[pending.pk]),
                         {"amount": "5000", "payer_phone": "0712345678", "transaction_reference": reference})
        return ManualMembershipPayment.objects.get(member__phone_number=PHONE, status="PENDING_VERIFICATION")

    def review(self, claim, action, **data):
        url = reverse("administration:membership_payment", args=[claim.pk])
        if action == "approve":
            data = {"verified_amount": "5000", "verified_reference": claim.transaction_reference,
                    "receipt_confirmed": "on", "recipient_confirmed": "on", **data}
        return self.staff.post(url, {"action": action, **data})

    def test_signup_to_paid_membership_and_included_video(self):
        member = self.sign_up()
        self.assertEqual((member.email, member.phone_verified), (None, False))
        self.assertTrue(identity_ready(member))
        dashboard = self.client.get(DASHBOARD)
        self.assertContains(dashboard, "Next step: choose a membership")
        self.assertNotContains(dashboard, "None")  # No empty email shown anywhere.

        claim = self.pay()
        self.assertContains(self.client.get(reverse("payments:manual_claim", args=[claim.pk])),
                            "Awaiting payment verification")
        self.assertEqual(videos.authorize_playback(member, self.video)[0], "no_membership")

        # Staff find the phone-only member by number, in any format, and see it in the queue.
        queue = self.staff.get(QUEUE, {"q": "0712 345 678", "status": "PENDING_VERIFICATION"})
        self.assertContains(queue, PHONE)
        self.assertContains(queue, claim.transaction_reference)
        self.assertRedirects(self.review(claim, "approve"),
                             reverse("administration:membership_payment", args=[claim.pk]))
        run_worker()

        member.refresh_from_db()
        self.assertEqual(check_access(member, "videos.standard").reason, "ok")
        self.assertEqual(videos.authorize_playback(member, self.video)[0], "ok")
        dashboard = self.client.get(DASHBOARD)
        self.assertContains(dashboard, "Payment update: confirmed")
        self.assertContains(dashboard, "Monthly is active until")
        self.assertContains(self.client.get(INBOX), "Payment confirmed: membership active")
        # No verified email: inbox only, never an email copy.
        self.assertFalse(OutboundEmail.objects.filter(user_id=member.pk).exists())
        self.assertEqual([m for m in mail.outbox if PHONE in str(m.to)], [])
        # The audit trail names the phone-only member by number.
        self.assertTrue(AuditEvent.objects.filter(action="membership.manual_payment.submitted",
                                                  actor_email=PHONE).exists())
        self.assertEqual(self.client.get(reverse("videos:detail", args=[self.video.slug])).status_code, 200)

    def test_rejection_shows_on_dashboard_and_inbox_without_email(self):
        member = self.sign_up()
        claim = self.pay()
        self.review(claim, "reject", reason="NOT_FOUND")
        run_worker()
        dashboard = self.client.get(DASHBOARD)
        self.assertContains(dashboard, "Payment update: not confirmed")
        self.assertContains(dashboard, "No matching transaction")
        self.assertTrue(Notification.objects.filter(recipient=member, kind="membership.payment_rejected").exists())
        self.assertFalse(OutboundEmail.objects.filter(user_id=member.pk).exists())
        self.assertEqual(check_access(member, "videos.standard").reason, "no_membership")

    def test_verified_email_gets_copies_and_enables_reset_by_email(self):
        member = self.sign_up()
        # Add an email from the account page; it counts only once confirmed.
        self.client.post(reverse("accounts:email_change"), {"new_email": "asha@example.com",
                                                            "current_password": NEW_PASSWORD})
        run_worker()
        link = [link for link in links_in_last_email() if "/accounts/email-change/" in link][0]
        opened = self.client.get(link)
        self.client.post(opened.url, {"current_password": NEW_PASSWORD})
        member.refresh_from_db()
        self.assertEqual((member.email, member.email_verified, member.phone_number), ("asha@example.com", True, PHONE))

        claim = self.pay()
        self.review(claim, "approve")
        run_worker()
        self.assertTrue(OutboundEmail.objects.filter(user_id=member.pk, kind="notifications.notice",
                                                     recipient="asha@example.com").exists())

        self.client.logout()
        mail.outbox.clear()
        self.client.post(RESET, {"email": "asha@example.com"})
        run_worker()
        self.assertTrue(any("/accounts/reset/" in link for link in links_in_last_email()))
        # Both identifiers sign in.
        for identifier in ("asha@example.com", "0712345678"):
            client = self.client_class()
            self.assertRedirects(client.post(LOGIN, {"username": identifier, "password": NEW_PASSWORD}), DASHBOARD)


class RecoveryTests(ManualPaymentTestCase):
    def test_phone_only_member_is_offered_support_not_a_phone_reset(self):
        User.objects.create_user(None, PASSWORD, phone_number=PHONE, status=User.Status.ACTIVE)
        page = self.client.get(RESET)
        self.assertContains(page, "Password reset by SMS is not available")
        self.assertContains(page, "Contact ClipCash support")
        self.assertNotContains(page, 'name="phone_number"')
        mail.outbox.clear()
        for typed in ("0712345678", "+255712345678"):  # Not an email: the form refuses it; nothing is sent.
            self.assertEqual(self.client.post(RESET, {"email": typed}).status_code, 200)
        run_worker()
        self.assertEqual(mail.outbox, [])

    def test_unverified_email_never_receives_a_reset_link(self):
        user = make_user("unconfirmed@example.com", verified=False)
        mail.outbox.clear()
        self.assertRedirects(self.client.post(RESET, {"email": "unconfirmed@example.com"}),
                             reverse("accounts:password_reset_done"))
        run_worker()
        self.assertEqual(mail.outbox, [])
        self.assertTrue(user.has_usable_password())

    def test_staff_cannot_be_recovered_by_phone_either(self):
        User.objects.filter(pk=self.verifier.pk).update(phone_number="+255713111222")
        mail.outbox.clear()
        self.assertEqual(self.client.post(RESET, {"email": "0713111222"}).status_code, 200)
        self.assertEqual(mail.outbox, [])


class ExistingAccountsTests(ManualPaymentTestCase):
    def test_existing_member_and_staff_sign_in_with_email(self):
        for email in ("member@example.com", "verifier@example.com", "root@example.com"):
            with self.subTest(email=email):
                client = self.client_class()
                self.assertRedirects(client.post(LOGIN, {"username": email.upper(), "password": PASSWORD}),
                                     DASHBOARD)
        self.member.refresh_from_db()
        self.assertTrue(identity_ready(self.member))
        staff = self.client_class()
        staff.post(LOGIN, {"username": "verifier@example.com", "password": PASSWORD})
        self.assertEqual(staff.get(QUEUE).status_code, 200)

    def test_existing_member_keeps_id_and_records(self):
        claim = self.submit()
        self.approve(claim)
        before = self.member.pk
        self.member.refresh_from_db()
        self.assertEqual(self.member.pk, before)
        self.assertEqual(Subscription.objects.filter(member=self.member, status="ACTIVE").count(), 1)

    def test_phone_only_member_is_denied_staff_pages(self):
        User.objects.create_user(None, PASSWORD, phone_number=PHONE, status=User.Status.ACTIVE)
        self.client.post(LOGIN, {"username": "0712345678", "password": PASSWORD})
        self.assertTrue(get_user(self.client).is_authenticated)
        for url in (reverse("administration:overview"), QUEUE, reverse("admin:index")):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response["Location"].startswith(reverse("admin:login")))

    def test_failed_sign_ins_share_one_limit_across_number_formats(self):
        User.objects.create_user(None, PASSWORD, phone_number=PHONE, status=User.Status.ACTIVE)
        cache.clear()
        for typed in ("0712345678", "+255712345678", "255712345678", "0712 345 678", "+255 712 345 678"):
            self.client.post(LOGIN, {"username": typed, "password": "wrong-password"})
        response = self.client.post(LOGIN, {"username": "0712345678", "password": PASSWORD})
        self.assertEqual([e.code for e in response.context["form"].non_field_errors().as_data()], ["throttled"])

    def test_unverified_email_only_account_still_needs_confirmation(self):
        self.client.force_login(make_user("legacy@example.com", verified=False))
        self.assertRedirects(self.client.get(DASHBOARD), reverse("accounts:verification_required"))

    def test_database_requires_a_sign_in_identifier(self):
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=self.member.pk).update(email=None)
        self.assertEqual(BUSINESS_NUMBER, "123456")  # Test records only.
