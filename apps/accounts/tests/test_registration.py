"""
REG-02: phone-first registration. Create account asks for an active Tanzanian mobile
number and a password; the account is ACTIVE at once, signed in, and lands on the
dashboard. Email is optional. One number, written any way, is one account.
"""

from django.contrib.auth import get_user
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse

from apps.audit.models import AuditEvent

from .helpers import NEW_PASSWORD, PASSWORD, AccountsTestCase, User, make_user

REGISTER_URL = reverse("accounts:register")
LOGIN_URL = reverse("accounts:login")
DASHBOARD = reverse("dashboard")
OPEN = override_settings(RESTRICTED_DEMO_MODE=False, PUBLIC_REGISTRATION_APPROVED=True)


@OPEN
class PhoneRegistrationTests(AccountsTestCase):
    def register(self, phone="0712 345 678", password=NEW_PASSWORD, confirm=None, client=None, **extra):
        return (client or self.client).post(REGISTER_URL, {
            "phone_number": phone, "password1": password,
            "password2": password if confirm is None else confirm, **extra})

    def test_form_asks_for_mobile_number_and_password_only(self):
        response = self.client.get(REGISTER_URL)
        for name in ("phone_number", "password1", "password2"):
            self.assertContains(response, f'name="{name}"')
        for name in ("email", "role", "status", "is_staff"):
            self.assertNotContains(response, f'name="{name}"')

    def test_creates_active_member_signed_in_on_the_dashboard(self):
        from apps.memberships.tests.helpers import enable_memberships

        enable_memberships()
        response = self.register("0712 345 678")
        self.assertRedirects(response, DASHBOARD)
        user = User.objects.get()
        self.assertEqual((user.phone_number, user.email, user.role, user.status),
                         ("+255712345678", None, User.Role.MEMBER, User.Status.ACTIVE))
        self.assertFalse(user.phone_verified or user.email_verified or user.is_staff or user.is_superuser)
        self.assertTrue(user.check_password(NEW_PASSWORD))
        self.assertEqual(get_user(self.client).pk, user.pk)
        self.assertContains(self.client.get(DASHBOARD), "Next step: choose a membership")
        self.assertTrue(AuditEvent.objects.filter(action="account.registered", target_id=str(user.pk)).exists())

    def test_every_accepted_format_is_one_number(self):
        self.register("0712345678")
        for variant in ("+255712345678", "255712345678", "+255 712-345-678", "00255712345678", "0712 345 678"):
            with self.subTest(variant=variant):
                cache.clear()  # Each attempt within the per-number limit.
                response = self.register(variant, client=self.client_class())
                self.assertContains(response, "An account already uses this mobile number")
        self.assertEqual(User.objects.count(), 1)
        self.assertTrue(AuditEvent.objects.filter(action="account.registration.duplicate").exists())

    def test_number_of_an_existing_email_account_is_refused(self):
        make_user("member@example.com")
        User.objects.filter(email="member@example.com").update(phone_number="+255754000111")
        self.assertContains(self.register("0754 000 111"), "An account already uses this mobile number")
        self.assertEqual(User.objects.count(), 1)

    def test_only_tanzanian_mobile_numbers(self):
        for bad in ("0222 123 456", "+254712345678", "0812345678", "12345", "", "not a number"):
            with self.subTest(phone=bad):
                self.assertEqual(self.register(bad).status_code, 200)
        self.assertFalse(User.objects.exists())

    def test_password_rules_apply(self):
        cases = {"mismatch": ("Strong-Pass-8812", "Other-Pass-8812"), "short": ("ab1!", None),
                 "common": ("password123", None), "numeric": ("83749201384", None),
                 "like the number": ("0712345678", None)}
        for name, (password, confirm) in cases.items():
            with self.subTest(name):
                response = self.register(password=password, confirm=confirm)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors)
        self.assertFalse(User.objects.exists())

    def test_injected_privilege_fields_are_ignored(self):
        self.register(role=User.Role.SUPER_ADMIN, status=User.Status.SUSPENDED, is_superuser="on",
                      email_verified="on", phone_verified="on", email="planted@example.com")
        user = User.objects.get()
        self.assertEqual((user.role, user.status, user.email), (User.Role.MEMBER, User.Status.ACTIVE, None))
        self.assertFalse(user.is_superuser or user.email_verified or user.phone_verified)

    def test_signs_in_again_with_any_format_of_the_number(self):
        self.register("0712 345 678")
        for identifier in ("0712345678", "+255712345678", "255 712 345 678"):
            with self.subTest(identifier=identifier):
                client = self.client_class()
                self.assertRedirects(client.post(LOGIN_URL, {"username": identifier, "password": NEW_PASSWORD}),
                                     DASHBOARD)
        client = self.client_class()
        self.assertEqual(client.post(LOGIN_URL, {"username": "0712345678", "password": PASSWORD}).status_code, 200)
        self.assertFalse(get_user(client).is_authenticated)

    def test_authenticated_user_redirected(self):
        self.client.force_login(make_user())
        self.assertRedirects(self.client.get(REGISTER_URL), reverse("accounts:profile"))

    def test_rate_limited_per_ip_and_per_number(self):
        for index in range(10):  # Fresh clients: a successful sign-up signs the browser in.
            self.register(f"07120000{index:02d}", client=self.client_class())
        self.assertContains(self.register("0712999999", client=self.client_class()), "Too many requests")
        self.assertFalse(User.objects.filter(phone_number="+255712999999").exists())
        cache.clear()
        for _ in range(5):
            self.register("0755111222", client=self.client_class(REMOTE_ADDR=f"198.51.100.{_ + 1}"))
        response = self.register("0755111222", client=self.client_class(REMOTE_ADDR="198.51.100.99"))
        self.assertContains(response, "Too many requests")


@override_settings(RESTRICTED_DEMO_MODE=True, PUBLIC_REGISTRATION_APPROVED=True)
class ClosedRegistrationTests(AccountsTestCase):
    def test_restricted_demo_refuses_registration(self):
        response = self.client.post(REGISTER_URL, {"phone_number": "0712345678", "password1": NEW_PASSWORD,
                                                   "password2": NEW_PASSWORD})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.exists())


class ConcurrentPhoneRegistrationTests(AccountsTestCase):
    def test_service_reports_a_taken_number(self):
        from apps.accounts import services

        services.register_with_phone(phone_number="+255712345678", password=NEW_PASSWORD)
        with self.assertRaises(services.DuplicatePhoneNumber):
            services.register_with_phone(phone_number="+255712345678", password=NEW_PASSWORD)
        self.assertEqual(User.objects.count(), 1)
