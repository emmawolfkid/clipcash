"""PAY-LIVE-01F-B: proving ownership of a mobile number with a one-time SMS code.

Uses the in-memory fake transport (apps.accounts.sms), enabled per test with
SMS_FAKE_TRANSPORT_ALLOWED; production keeps SMS disabled and refuses the fake transport.
"""

import json
import logging
import re
import threading
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connections, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.test.client import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.accounts import phone_verification as pv
from apps.accounts import sms, throttling
from apps.accounts.access import has_verified_contact
from apps.accounts.checks import check_sms_transport
from apps.accounts.models import PhoneVerificationChallenge as Challenge
from apps.audit.models import AuditEvent
from apps.memberships import services as memberships
from apps.memberships.tests.helpers import MembershipTestCase

from .helpers import PASSWORD, AccountsTestCase, User, make_user

FAKE = override_settings(SMS_BACKEND="fake", SMS_FAKE_TRANSPORT_ALLOWED=True)
PHONE = "+255712345678"
OTHER_PHONE = "+255765432100"
VERIFY = reverse("accounts:phone_verify")


def phone_user(phone=PHONE):
    return User.objects.create_user(None, PASSWORD, phone_number=phone, status=User.Status.ACTIVE)


def last_code():
    return re.search(r"\b([0-9]{6})\b", sms.outbox[-1][1]).group(1)


def wrong(code):
    return f"{(int(code) + 1) % 1_000_000:06d}"


def age_challenges(user, seconds=3600):
    """Move the account's challenges back in time (past the resend cooldown)."""
    for challenge in Challenge.objects.filter(user=user):
        Challenge.objects.filter(pk=challenge.pk).update(created_at=challenge.created_at - timedelta(seconds=seconds))


class SmsTestMixin:
    def setUp(self):
        super().setUp()
        cache.clear()
        sms.outbox.clear()
        self.user = phone_user()
        self.request = RequestFactory().post(VERIFY, REMOTE_ADDR="203.0.113.7")

    def send(self, user=None):
        return pv.request_phone_verification(user or self.user, self.request)

    def confirm(self, code, user=None):
        return pv.confirm_phone_verification(user or self.user, code, self.request)

    def refreshed(self):
        self.user.refresh_from_db()
        return self.user


# --- Configuration -----------------------------------------------------------------------------


class ConfigurationTests(SmsTestMixin, AccountsTestCase):
    def test_disabled_by_default_nothing_is_sent_or_stored(self):
        self.assertFalse(pv.sms_delivery_available())
        with self.assertRaises(pv.PhoneVerificationUnavailable):
            self.send()
        self.assertFalse(Challenge.objects.exists())
        self.assertEqual(sms.outbox, [])
        self.client.force_login(self.user)
        page = self.client.get(VERIFY)
        self.assertContains(page, "Phone verification by SMS is not available yet")
        self.assertEqual(self.client.post(VERIFY, {"action": "send"}).status_code, 503)

    @override_settings(SMS_BACKEND="fake", SMS_FAKE_TRANSPORT_ALLOWED=False)
    def test_the_fake_transport_never_works_outside_debug(self):
        self.assertFalse(pv.sms_delivery_available())
        with self.assertRaises(pv.PhoneVerificationUnavailable):
            self.send()

    def test_deploy_check_refuses_fake_and_unknown_backends(self):
        with override_settings(SMS_BACKEND="fake", DEBUG=False):
            self.assertEqual([e.id for e in check_sms_transport(None)], ["accounts.E009"])
        with override_settings(SMS_BACKEND="some-provider"):
            self.assertEqual([e.id for e in check_sms_transport(None)], ["accounts.E009"])
        with override_settings(SMS_BACKEND="disabled"):
            self.assertEqual(check_sms_transport(None), [])

    @FAKE
    def test_undeliverable_message_closes_the_challenge(self):
        with mock.patch.object(sms.FakeTransport, "send", side_effect=sms.SmsDeliveryError("down")):
            with self.assertRaises(pv.PhoneVerificationUnavailable):
                self.send()
        self.assertEqual(Challenge.objects.get().status, Challenge.Status.UNDELIVERED)
        self.assertTrue(AuditEvent.objects.filter(action="account.phone_verification.refused",
                                                  metadata__reason="undelivered").exists())


# --- Codes ------------------------------------------------------------------------------------------


@FAKE
class CodeTests(SmsTestMixin, AccountsTestCase):
    def test_correct_code_verifies_the_number_once(self):
        challenge = self.send()
        self.assertEqual(sms.outbox[-1][0], PHONE)
        code = last_code()
        stored = Challenge.objects.get(pk=challenge.pk)
        self.assertNotIn(code, stored.code_hash)
        self.assertRegex(stored.code_hash, r"^[0-9a-f]{64}$")
        self.assertLessEqual(stored.expires_at, timezone.now() + timedelta(minutes=5))
        self.confirm(code)
        user = self.refreshed()
        self.assertTrue(user.phone_verified)
        self.assertTrue(has_verified_contact(user))
        self.assertEqual(Challenge.objects.get(pk=challenge.pk).status, Challenge.Status.VERIFIED)
        self.assertTrue(AuditEvent.objects.filter(action="account.phone_verified").exists())
        with self.assertRaises(pv.InvalidCode):  # Single use: a replay finds nothing to verify.
            self.confirm(code)

    def test_incorrect_and_malformed_codes_are_refused_and_counted(self):
        self.send()
        code = last_code()
        for bad in (wrong(code), "12345", "abcdef", ""):
            with self.subTest(bad=bad), self.assertRaises(pv.InvalidCode):
                self.confirm(bad)
        self.assertEqual(Challenge.objects.get().attempts, 4)
        self.assertFalse(self.refreshed().phone_verified)
        self.confirm(f"{code[:3]} {code[3:]}")  # Spaces are ignored; still within the attempt limit.
        self.assertTrue(self.refreshed().phone_verified)

    def test_attempt_limit_closes_the_challenge(self):
        self.send()
        code = last_code()
        for _ in range(4):
            with self.assertRaises(pv.InvalidCode):
                self.confirm(wrong(code))
        with self.assertRaises(pv.AttemptsExhausted):
            self.confirm(wrong(code))
        self.assertEqual(Challenge.objects.get().status, Challenge.Status.EXHAUSTED)
        with self.assertRaises(pv.InvalidCode):  # Even the right code no longer works.
            self.confirm(code)
        self.assertFalse(self.refreshed().phone_verified)

    def test_expired_code_is_refused(self):
        self.send()
        code = last_code()
        Challenge.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(pv.InvalidCode):
            self.confirm(code)
        self.assertEqual(Challenge.objects.get().status, Challenge.Status.EXPIRED)
        self.assertFalse(self.refreshed().phone_verified)

    def test_new_code_replaces_the_old_one(self):
        self.send()
        old = last_code()
        age_challenges(self.user)
        self.send()
        new = last_code()
        self.assertEqual(Challenge.objects.filter(status=Challenge.Status.ACTIVE).count(), 1)
        if old != new:
            with self.assertRaises(pv.InvalidCode):
                self.confirm(old)
        self.confirm(new)
        self.assertTrue(self.refreshed().phone_verified)

    def test_codes_are_unpredictable_six_digits(self):
        codes = set()
        for _ in range(5):
            age_challenges(self.user)
            cache.clear()
            self.send()
            codes.add(last_code())
        self.assertTrue(all(re.fullmatch(r"[0-9]{6}", code) for code in codes))
        self.assertGreater(len(codes), 1)


# --- Throttling ----------------------------------------------------------------------------------


@FAKE
class ThrottleTests(SmsTestMixin, AccountsTestCase):
    def test_resend_cooldown(self):
        self.send()
        with self.assertRaises(pv.RateLimited) as caught:
            self.send()
        self.assertEqual(caught.exception.code, "cooldown")
        self.assertTrue(0 < caught.exception.retry_after <= 60)
        self.assertEqual(len(sms.outbox), 1)

    def test_hourly_limit_per_account(self):
        for _ in range(5):
            age_challenges(self.user)
            self.send()
        age_challenges(self.user)
        with self.assertRaises(pv.RateLimited) as caught:
            self.send()
        self.assertEqual(caught.exception.code, "hourly_limit")
        self.assertEqual(len(sms.outbox), 5)

    def test_hourly_limit_per_number_and_per_ip(self):
        for _ in range(throttling.PHONE_CODES_PER_NUMBER.limit):
            throttling.PHONE_CODES_PER_NUMBER.hit(PHONE)
        with self.assertRaises(pv.RateLimited):
            self.send()
        cache.clear()
        other = phone_user(OTHER_PHONE)
        for _ in range(throttling.PHONE_CODES_PER_IP.limit):
            throttling.PHONE_CODES_PER_IP.hit("203.0.113.7")
        with self.assertRaises(pv.RateLimited):
            self.send(other)
        self.assertEqual(sms.outbox, [])

    def test_code_checks_are_limited_per_ip(self):
        self.send()
        code = last_code()
        for _ in range(throttling.PHONE_CODE_CHECKS_PER_IP.limit):
            throttling.PHONE_CODE_CHECKS_PER_IP.hit("203.0.113.7")
        with self.assertRaises(pv.RateLimited):
            self.confirm(code)
        self.assertFalse(self.refreshed().phone_verified)


# --- Identity ------------------------------------------------------------------------------------


@FAKE
class IdentityTests(SmsTestMixin, AccountsTestCase):
    def test_a_code_for_the_old_number_never_verifies_a_new_number(self):
        self.send()
        code = last_code()
        self.user.phone_number = OTHER_PHONE
        self.user.save()
        with self.assertRaises(pv.InvalidCode):
            self.confirm(code)
        self.assertFalse(self.refreshed().phone_verified)
        self.assertEqual(Challenge.objects.get().status, Challenge.Status.SUPERSEDED)

    def test_changing_a_verified_number_removes_the_verification(self):
        self.send()
        self.confirm(last_code())
        user = self.refreshed()
        user.phone_number = OTHER_PHONE
        user.save()
        user.refresh_from_db()
        self.assertFalse(user.phone_verified)
        self.assertFalse(has_verified_contact(user))

    def test_a_number_belongs_to_one_account(self):
        self.send()
        self.confirm(last_code())
        with self.assertRaises((IntegrityError, ValidationError)), transaction.atomic():
            phone_user(PHONE)
        self.assertEqual(User.objects.filter(phone_number=PHONE).count(), 1)

    def test_nothing_to_verify(self):
        email_only = make_user("email@example.com")
        with self.assertRaises(pv.NothingToVerify):
            self.send(email_only)
        self.send()
        self.confirm(last_code())
        with self.assertRaises(pv.NothingToVerify):
            self.send()

    def test_email_based_accounts_are_unchanged(self):
        email_user = make_user("asha@example.com")  # Confirmed email.
        self.assertTrue(has_verified_contact(email_user))
        self.assertFalse(email_user.phone_verified)
        unconfirmed = make_user("new@example.com", verified=False)
        self.assertFalse(has_verified_contact(unconfirmed))

    def test_recovery_by_sms_stays_unavailable(self):
        self.send()
        self.confirm(last_code())
        with self.assertRaises(pv.PhoneVerificationUnavailable):
            pv.request_phone_recovery(PHONE)

    def test_no_code_in_storage_audit_or_logs(self):
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("clipcash").debug("probe")
            self.send()
            code = last_code()
            with self.assertRaises(pv.InvalidCode):
                self.confirm(wrong(code))
            self.confirm(code)
        self.assertNotIn(code, "\n".join(logs.output))
        for event in AuditEvent.objects.filter(action__startswith="account.phone_verif"):
            self.assertNotIn(code, json.dumps(event.metadata))
            self.assertNotIn(PHONE, json.dumps(event.metadata))
        challenge = Challenge.objects.get()
        self.assertNotIn(code, challenge.code_hash)
        self.assertNotIn(PHONE, str(challenge))

    def test_prune_removes_old_closed_challenges(self):
        self.send()
        self.confirm(last_code())
        self.assertEqual(pv.prune_challenges(), 0)
        self.assertEqual(pv.prune_challenges(now=timezone.now() + timedelta(days=2)), 1)
        self.assertFalse(Challenge.objects.exists())


@FAKE
class MembershipEligibilityTests(SmsTestMixin, MembershipTestCase):
    def test_verified_phone_can_start_a_paid_membership(self):
        with self.assertRaises(memberships.NotEligible) as caught:
            memberships.choose_plan(self.user, self.plan)
        self.assertEqual(caught.exception.code, "contact_unverified")
        self.send()
        self.confirm(last_code())
        pending = memberships.choose_plan(self.refreshed(), self.plan)
        self.assertEqual((pending.status, pending.price_amount), ("PENDING", self.plan.price_amount))

    @mock.patch("apps.memberships.views.purchase_open", return_value=True)  # Shown only while buying is open.
    def test_plan_page_offers_phone_verification(self, _open):
        self.client.force_login(self.user)
        page = self.client.get(reverse("memberships:plan_detail", args=[self.plan.code]))
        self.assertContains(page, "Verify your mobile number to buy a membership")
        self.assertContains(page, VERIFY)


# --- Pages -------------------------------------------------------------------------------------------


@FAKE
class PageTests(SmsTestMixin, AccountsTestCase):
    def test_send_enter_and_verify_without_echoing_the_code(self):
        self.client.force_login(self.user)
        start = self.client.get(VERIFY)
        self.assertContains(start, "Send code")
        self.assertContains(start, PHONE)
        sent = self.client.post(VERIFY, {"action": "send"}, follow=True)
        self.assertContains(sent, "a code is on its way")
        self.assertContains(sent, "Verification code")
        code = last_code()
        self.assertNotContains(sent, code)
        bad = self.client.post(VERIFY, {"action": "confirm", "code": wrong(code)})
        self.assertEqual(bad.status_code, 400)
        self.assertContains(bad, "That code is not valid or has expired", status_code=400)
        self.assertNotContains(bad, wrong(code), status_code=400)
        self.assertNotContains(bad, code, status_code=400)
        done = self.client.post(VERIFY, {"action": "confirm", "code": code}, follow=True)
        self.assertRedirects(done, reverse("accounts:profile"))
        self.assertContains(done, "Your mobile number is verified.")
        self.assertNotContains(done, code)
        self.assertTrue(self.refreshed().phone_verified)

    def test_cooldown_and_exhaustion_messages(self):
        self.client.force_login(self.user)
        self.client.post(VERIFY, {"action": "send"})
        again = self.client.post(VERIFY, {"action": "send"})
        self.assertEqual(again.status_code, 429)
        self.assertContains(again, "Please wait", status_code=429)
        code = last_code()
        for _ in range(4):
            self.client.post(VERIFY, {"action": "confirm", "code": wrong(code)})
        last = self.client.post(VERIFY, {"action": "confirm", "code": wrong(code)})
        self.assertContains(last, "Too many incorrect codes", status_code=400)

    def test_anonymous_and_csrf(self):
        anonymous = self.client.get(VERIFY)
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn(reverse("accounts:login"), anonymous["Location"])
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(VERIFY, {"action": "send"}).status_code, 403)
        self.assertEqual(sms.outbox, [])

    def test_profile_links_to_verification(self):
        self.client.force_login(self.user)
        profile = self.client.get(reverse("accounts:profile"))
        self.assertContains(profile, "Verify by SMS")
        self.assertContains(profile, "passwords are reset by email only")


@FAKE
class ConcurrentVerificationTests(TransactionTestCase):
    """Two submissions of the right code at the same moment: verified once, the other refused."""

    def setUp(self):
        cache.clear()
        sms.outbox.clear()
        self.user = phone_user()

    def test_concurrent_confirmations_use_the_code_once(self):
        pv.request_phone_verification(self.user)
        code = last_code()
        barrier = threading.Barrier(2)
        results = []

        def run():
            try:
                barrier.wait(5)
                results.append(pv.confirm_phone_verification(self.user, code))
            except Exception as error:  # noqa: BLE001 - collected and asserted below
                results.append(error)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        verified = [r for r in results if isinstance(r, User)]
        refused = [r for r in results if isinstance(r, pv.InvalidCode)]
        self.assertEqual((len(verified), len(refused)), (1, 1), results)
        self.assertEqual(Challenge.objects.get().status, Challenge.Status.VERIFIED)
        self.assertEqual(AuditEvent.objects.filter(action="account.phone_verified").count(), 1)

    def test_concurrent_requests_send_one_code(self):
        barrier = threading.Barrier(2)
        results = []

        def run():
            try:
                barrier.wait(5)
                results.append(pv.request_phone_verification(self.user))
            except Exception as error:  # noqa: BLE001
                results.append(error)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len([r for r in results if isinstance(r, Challenge)]), 1, results)
        self.assertEqual(len([r for r in results if isinstance(r, pv.RateLimited)]), 1, results)
        self.assertEqual(Challenge.objects.filter(status=Challenge.Status.ACTIVE).count(), 1)
        self.assertEqual(len(sms.outbox), 1)
