"""PAY-01: production self-registration. Opens only with an explicit approval
(DJANGO_PUBLIC_REGISTRATION_APPROVED) outside the restricted demonstration, and a
new account never starts with a paid membership or any way to activate one itself."""

import json

from django.core import mail
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.core import features
from apps.core.tests.test_deployment import run_python
from apps.memberships import services as memberships
from apps.memberships.access import check_access
from apps.memberships.models import Subscription
from apps.memberships.tests.helpers import enable_memberships

from .helpers import NEW_PASSWORD, AccountsTestCase, User, links_in_last_email, make_user

REGISTER_URL = reverse("accounts:register")
PRODUCTION = override_settings(RESTRICTED_DEMO_MODE=False, PUBLIC_REGISTRATION_APPROVED=True,
                               MEMBERSHIPS_COMPLIMENTARY_GRANTS_ENABLED=True)


class RegistrationGateTests(AccountsTestCase):
    def test_registration_needs_explicit_approval_outside_the_demo(self):
        cases = (
            ({"RESTRICTED_DEMO_MODE": False, "PUBLIC_REGISTRATION_APPROVED": True}, True),
            ({"RESTRICTED_DEMO_MODE": False, "PUBLIC_REGISTRATION_APPROVED": False}, False),
            ({"RESTRICTED_DEMO_MODE": True, "PUBLIC_REGISTRATION_APPROVED": True}, False),
            ({"RESTRICTED_DEMO_MODE": True, "PUBLIC_REGISTRATION_APPROVED": False}, False),
        )
        for overrides, open_ in cases:
            with self.subTest(**overrides), override_settings(**overrides):
                self.client.logout()  # A successful sign-up signs the browser in.
                self.assertEqual(features.is_enabled("registration"), open_)
                response = self.client.get(REGISTER_URL)
                self.assertEqual(response.status_code, 200 if open_ else 403)
                response = self.client.post(REGISTER_URL, {"phone_number": "0712345678", "password1": NEW_PASSWORD,
                                                           "password2": NEW_PASSWORD})
                self.assertIn(response.status_code, (302, 200) if open_ else (403,))
        self.assertEqual(User.objects.filter(phone_number="+255712345678").count(), 1)

    @PRODUCTION
    def test_site_configuration_can_still_close_registration(self):
        from apps.core.models import SiteConfiguration
        from django.core.cache import cache

        SiteConfiguration.objects.update_or_create(id=1, defaults={"registration_enabled": False})
        cache.clear()
        self.assertFalse(features.is_enabled("registration"))
        self.assertEqual(self.client.get(REGISTER_URL).status_code, 403)


@PRODUCTION
class ProductionRegistrationJourneyTests(AccountsTestCase):
    def setUp(self):
        super().setUp()
        enable_memberships()

    def register(self, phone="0712 345 678", password=NEW_PASSWORD):
        return self.client.post(REGISTER_URL, {"phone_number": phone, "password1": password, "password2": password})

    def test_register_sign_in_and_start_without_membership(self):
        # REG-02: phone-first, active at once, no paid privileges.
        self.assertRedirects(self.register(), reverse("dashboard"))
        user = User.objects.get(phone_number="+255712345678")
        self.assertEqual((user.role, user.status, user.email), (User.Role.MEMBER, User.Status.ACTIVE, None))
        self.assertTrue(AuditEvent.objects.filter(action="account.registered", target_id=str(user.pk)).exists())
        self.assertFalse(Subscription.objects.filter(member=user).exists())
        self.assertEqual(check_access(user).reason, "no_membership")
        self.assertContains(self.client.get(reverse("dashboard")), "You do not have an active membership.")

    def test_weak_passwords_get_clear_messages(self):
        self.assertContains(self.register(password="12345"), "This password is too short")
        self.assertFalse(User.objects.exists())

    def test_duplicate_registration_creates_no_second_account(self):
        self.register("0712345678")
        self.client.logout()
        self.assertContains(self.register("+255 712 345 678"), "An account already uses this mobile number")
        self.assertEqual(User.objects.filter(phone_number="+255712345678").count(), 1)
        self.assertTrue(AuditEvent.objects.filter(action="account.registration.duplicate").exists())

    def test_password_reset_is_available(self):
        make_user("reset@example.com")
        mail.outbox.clear()
        response = self.client.post(reverse("accounts:password_reset"), {"email": "reset@example.com"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any("/accounts/reset/" in link for link in links_in_last_email()))

    def test_members_cannot_activate_a_membership_themselves(self):
        member = make_user("member@example.com")
        root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        plan = memberships.create_plan(root, code="monthly", name="Monthly", price_amount=5_000, duration_days=30)
        pending = memberships.choose_plan(member, plan)
        # No live verifier exists; the live_payments path stays locked.
        with self.assertRaises(memberships.PaymentVerificationError):
            memberships.activate_with_verified_payment(pending.pk, memberships.VerifiedPayment(
                verifier="anything", reference="X", amount=5_000, currency="TZS", confirmed_at=pending.created_at))
        # Complimentary grants exist only inside the restricted demonstration.
        self.assertFalse(memberships.complimentary_grants_available())
        with self.assertRaises(memberships.NotAuthorized):
            memberships.grant_complimentary(member, member, plan, expires_at=pending.pending_until, reason="Myself")
        self.client.force_login(member)
        response = self.client.post(reverse("administration:member_complimentary", args=[member.pk]))
        self.assertEqual(response.status_code, 302)  # Staff-only console: sent to the admin login.
        pending.refresh_from_db()
        self.assertEqual(pending.status, Subscription.Status.PENDING)
        self.assertFalse(check_access(member))


PROBE = """
import json, django
django.setup()
from django.conf import settings as s
from apps.core.features import is_enabled
print(json.dumps({"approved": s.PUBLIC_REGISTRATION_APPROVED, "manual": s.MANUAL_MEMBERSHIP_PAYMENTS_ENABLED,
                  "registration": is_enabled("registration"),
                  "manual_feature": is_enabled("manual_membership_payments")}))
"""


class ProductionDefaultsTests(SimpleTestCase):
    def probe(self, **overrides):
        result = run_python(["-c", PROBE], **overrides)
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def test_registration_and_manual_payments_are_off_by_default(self):
        self.assertEqual(self.probe(), {"approved": False, "manual": False, "registration": False,
                                        "manual_feature": False})
        # Leaving the demo is not enough to open registration.
        self.assertFalse(self.probe(DJANGO_RESTRICTED_DEMO="False")["registration"])

    def test_manual_payments_need_the_demo_switched_off(self):
        self.assertFalse(self.probe(DJANGO_MANUAL_MEMBERSHIP_PAYMENTS="True")["manual_feature"])
        values = self.probe(DJANGO_MANUAL_MEMBERSHIP_PAYMENTS="True", DJANGO_RESTRICTED_DEMO="False")
        self.assertTrue(values["manual_feature"])
