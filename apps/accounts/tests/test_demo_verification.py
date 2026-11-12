"""Restricted demo: optional email verification for approved demo accounts only."""

from django.contrib.auth import get_user
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django.urls import reverse

from apps.accounts.access import demo_verification_optional, email_verification_waived, role_required
from apps.audit.models import AuditEvent
from apps.core import features
from apps.payments.providers import sandbox_active

from .helpers import PASSWORD, AccountsTestCase, User, make_user

LOGIN = reverse("accounts:login")
PROFILE = reverse("accounts:profile")
DASHBOARD = reverse("dashboard")
ACTIVITY = reverse("activity")
REGISTER = reverse("accounts:register")
VERIFY = reverse("accounts:verification_required")
CONSOLE = reverse("administration:overview")
MEMBER_PAGES = (PROFILE, DASHBOARD, ACTIVITY)
DEMO_EMAIL = "demo@example.com"
BANNER = "Email verification is optional for authorized demo accounts. Real payments and payouts are disabled."

DEMO = override_settings(
    RESTRICTED_DEMO_MODE=True,
    DEMO_EMAIL_VERIFICATION_OPTIONAL=True,
    DEMO_ACCOUNTS=[" Demo@Example.com ", "staff-demo@example.com"],  # Normalized when read.
    PAYMENTS_MODE="disabled",
    PAYMENTS_SANDBOX_ALLOWED=False,
)


class DemoTestCase(AccountsTestCase):
    def setUp(self):
        super().setUp()
        self.demo = make_user(DEMO_EMAIL, verified=False)

    def sign_in(self, email=DEMO_EMAIL, password=PASSWORD):
        return self.client.post(LOGIN, {"username": email, "password": password})

    def assert_member_pages(self, *, allowed):
        for url in MEMBER_PAGES:
            with self.subTest(url=url):
                response = self.client.get(url)
                if allowed:
                    self.assertEqual(response.status_code, 200)
                else:
                    self.assertRedirects(response, VERIFY)


@DEMO
class ApprovedDemoAccountTests(DemoTestCase):
    def test_signs_in_and_uses_member_pages_without_verified_email(self):  # 1
        self.assertRedirects(self.sign_in(), DASHBOARD)  # REG-01: sign-in lands on the dashboard.
        self.assert_member_pages(allowed=True)

    def test_email_stays_unverified(self):  # Never marked verified.
        self.sign_in()
        self.assert_member_pages(allowed=True)
        self.demo.refresh_from_db()
        self.assertFalse(self.demo.email_verified)

    def test_verification_flow_still_available(self):
        self.client.force_login(self.demo)
        self.assertEqual(self.client.get(VERIFY).status_code, 200)
        self.assertRedirects(self.client.post(reverse("accounts:resend_verification")), VERIFY)

    def test_notice_shown(self):  # Task 4
        self.assertContains(self.client.get(reverse("home")), BANNER)
        self.client.force_login(self.demo)
        self.assertContains(self.client.get(DASHBOARD), BANNER)


@DEMO
class UnauthorizedAccountTests(DemoTestCase):
    def test_unlisted_unverified_member_still_redirected(self):  # 2
        self.client.force_login(make_user("other@example.com", verified=False))
        self.assert_member_pages(allowed=False)

    def test_unlisted_unverified_super_admin_still_redirected(self):
        self.client.force_login(make_user("root@example.com", role=User.Role.SUPER_ADMIN, verified=False))
        self.assert_member_pages(allowed=False)

    def test_listed_but_not_active_cannot_sign_in(self):
        for status in (User.Status.PENDING, User.Status.SUSPENDED, User.Status.DISABLED):
            with self.subTest(status=status):
                self.demo.status = status
                self.demo.save()  # Derives is_active from status.
                self.sign_in()
                self.assertFalse(get_user(self.client).is_authenticated)
                self.assertFalse(email_verification_waived(self.demo))

    def test_anonymous_never_waived(self):
        self.assertRedirects(self.client.get(DASHBOARD), f"{LOGIN}?next={DASHBOARD}")

    def test_registration_still_blocked(self):  # 3
        self.assertEqual(self.client.get(REGISTER).status_code, 403)
        self.assertEqual(self.client.post(REGISTER, {"email": "new@example.com"}).status_code, 403)
        self.assertFalse(User.objects.filter(email="new@example.com").exists())
        self.assertNotContains(self.client.get(reverse("home")), REGISTER)


class ProductionVerificationTests(DemoTestCase):
    @override_settings(RESTRICTED_DEMO_MODE=False, DEMO_EMAIL_VERIFICATION_OPTIONAL=False,
                       DEMO_ACCOUNTS=[DEMO_EMAIL])
    def test_normal_mode_requires_verification(self):  # 4
        self.client.force_login(self.demo)
        self.assert_member_pages(allowed=False)
        self.assertNotContains(self.client.get(reverse("home")), BANNER)

    @override_settings(RESTRICTED_DEMO_MODE=False, DEMO_EMAIL_VERIFICATION_OPTIONAL=True,
                       DEMO_ACCOUNTS=[DEMO_EMAIL], PAYMENTS_MODE="disabled")
    def test_flag_alone_does_not_bypass(self):  # 5
        self.assertFalse(demo_verification_optional())
        self.client.force_login(self.demo)
        self.assert_member_pages(allowed=False)
        self.assertNotContains(self.client.get(reverse("home")), BANNER)

    @override_settings(DEBUG=False, RESTRICTED_DEMO_MODE=False, DEMO_EMAIL_VERIFICATION_OPTIONAL=True)
    def test_deploy_check_warns_about_ineffective_flag(self):
        from apps.accounts.checks import check_production_security

        self.assertIn("accounts.W001", [message.id for message in check_production_security(None)])

    @override_settings(RESTRICTED_DEMO_MODE=True, DEMO_EMAIL_VERIFICATION_OPTIONAL=True, DEMO_ACCOUNTS=[],
                       PAYMENTS_MODE="disabled")
    def test_empty_allowlist_waives_nobody(self):
        self.client.force_login(self.demo)
        self.assert_member_pages(allowed=False)

    @override_settings(RESTRICTED_DEMO_MODE=True, DEMO_EMAIL_VERIFICATION_OPTIONAL=True,
                       DEMO_ACCOUNTS=[DEMO_EMAIL], PAYMENTS_MODE="sandbox", PAYMENTS_SANDBOX_ALLOWED=True)
    def test_no_waiver_while_any_payments_are_possible(self):
        self.assertTrue(sandbox_active())
        self.assertFalse(demo_verification_optional())
        self.client.force_login(self.demo)
        self.assert_member_pages(allowed=False)

    def test_turning_the_flag_off_restores_verification_immediately(self):  # 10
        self.client.force_login(self.demo)
        with DEMO:
            self.assert_member_pages(allowed=True)
        with DEMO, self.settings(DEMO_EMAIL_VERIFICATION_OPTIONAL=False):
            self.assert_member_pages(allowed=False)
        with DEMO, self.settings(RESTRICTED_DEMO_MODE=False):
            self.assert_member_pages(allowed=False)
        with DEMO, self.settings(DEMO_ACCOUNTS=[]):
            self.assert_member_pages(allowed=False)
        self.assertTrue(get_user(self.client).is_authenticated)  # Same session throughout.


@DEMO
class RoleAndMoneyTests(DemoTestCase):
    def test_waiver_grants_no_staff_or_role_access(self):  # 6
        self.client.force_login(self.demo)
        response = self.client.get(CONSOLE)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(reverse("admin:login")))
        self.assertNotEqual(self.client.get("/admin/").status_code, 200)

        request = RequestFactory().get("/restricted/")
        request.user = self.demo
        view = role_required(User.Role.SUPER_ADMIN, User.Role.CONTENT_MANAGER)(lambda r: HttpResponse("ok"))
        with self.assertRaises(PermissionDenied):
            view(request)

    def test_listed_content_manager_keeps_only_its_own_role(self):
        manager = make_user("staff-demo@example.com", role=User.Role.CONTENT_MANAGER, verified=False)
        self.client.force_login(manager)
        self.assertEqual(self.client.get(DASHBOARD).status_code, 200)
        self.assertEqual(self.client.get(CONSOLE).status_code, 200)  # Staff already, unchanged.
        response = self.client.get(reverse("admin:core_siteconfiguration_changelist"))
        self.assertIn(response.status_code, (302, 403))  # Super Administrators only.

    def test_money_stays_disabled(self):  # 7
        self.assertFalse(sandbox_active())
        self.assertEqual([key for key in features.FINANCIAL_FEATURES if features.is_enabled(key)], [])
        self.client.force_login(self.demo)
        self.assertNotEqual(self.client.get(reverse("payments:history")).status_code, 200)

    def test_login_throttling_and_audit_unchanged(self):  # 8
        for _ in range(5):
            self.sign_in(password="wrong-password-123")
        response = self.sign_in()  # Correct password, still throttled.
        self.assertEqual(response.status_code, 200)
        self.assertEqual([e.code for e in response.context["form"].non_field_errors().as_data()], ["throttled"])
        self.assertFalse(get_user(self.client).is_authenticated)
        actions = set(AuditEvent.objects.values_list("action", flat=True))
        self.assertTrue({"auth.login.failed", "auth.login.throttled"} <= actions)

    def test_successful_login_and_logout_audited(self):
        self.sign_in()
        self.client.post(reverse("accounts:logout"))
        actions = set(AuditEvent.objects.values_list("action", flat=True))
        self.assertTrue({"auth.login.succeeded", "auth.logout"} <= actions)

    def test_csrf_still_enforced(self):
        client = self.client_class(enforce_csrf_checks=True)
        response = client.post(LOGIN, {"username": DEMO_EMAIL, "password": PASSWORD})
        self.assertEqual(response.status_code, 403)

    def test_restricted_demo_checks_still_pass(self):  # 9
        call_command("check", verbosity=0)
        self.assertFalse(features.is_enabled("registration"))
