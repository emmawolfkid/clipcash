"""
PRD-03B regression: existing and new accounts reach their dashboard, the old "Confirm your email"
URL never traps anyone, and Sign out is always reachable. Fictional records only.
"""

import re

from django.contrib.auth import get_user
from django.urls import reverse

from apps.accounts.tests.helpers import NEW_PASSWORD, PASSWORD, make_user
from apps.advertising import services as advertising
from apps.memberships import services as memberships
from apps.payments.tests.manual_helpers import ManualPaymentTestCase, User
from apps.wallets.models import Wallet

LOGIN = reverse("accounts:login")
LOGOUT = reverse("accounts:logout")
REGISTER = reverse("accounts:register")
DASHBOARD = reverse("dashboard")
PROFILE = reverse("accounts:profile")
VERIFY = reverse("accounts:verification_required")
MAX_REDIRECTS = 5


def follow(client, url, method="get", data=None):
    """Follow redirects by hand, refusing loops: returns (final response, visited paths)."""
    visited = []
    response = getattr(client, method)(url, data or {})
    while response.status_code in (301, 302, 303, 307, 308):
        location = response["Location"]
        visited.append(location)
        if len(visited) > MAX_REDIRECTS or visited.count(location) > 1:
            raise AssertionError(f"redirect loop: {visited}")
        response = client.get(location)
    return response, visited


class ExistingEmailAccountTests(ManualPaymentTestCase):
    """Accounts from before PRD-03A: email only, never confirmed, flag off (pre-REG-04 shape)."""

    def setUp(self):
        super().setUp()
        self.legacy = make_user("legacy@example.com", verified=False)
        self.assertFalse(self.legacy.email_ownership_required)

    def test_signs_in_through_the_form_and_lands_on_the_dashboard(self):
        response, visited = follow(self.client, LOGIN, "post", {"username": "legacy@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request["PATH_INFO"], DASHBOARD)
        self.assertNotIn(VERIFY, visited)

    def test_a_stale_next_to_the_verification_page_still_ends_on_a_usable_page(self):
        response, visited = follow(self.client, f"{LOGIN}?next={VERIFY}", "post",
                                   {"username": "legacy@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.request["PATH_INFO"], VERIFY)

    def test_a_session_from_before_the_change_keeps_working(self):
        self.client.force_login(self.legacy)  # Session created earlier; nothing about verification is stored.
        for url in (DASHBOARD, PROFILE, reverse("memberships:plans"), reverse("notifications:inbox")):
            with self.subTest(url):
                response, visited = follow(self.client, url)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(VERIFY, visited)

    def test_the_old_verification_url_leads_back_without_forcing_anything(self):
        self.client.force_login(self.legacy)
        response, visited = follow(self.client, VERIFY)
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.request["PATH_INFO"], VERIFY)

    def test_financial_restrictions_still_apply(self):
        with self.assertRaises(memberships.NotEligible):
            memberships.choose_plan(self.legacy, self.plan)
        self.client.force_login(self.legacy)
        self.client.post(reverse("wallets:wallet"))
        self.assertFalse(Wallet.objects.filter(member=self.legacy).exists())
        self.assertEqual(advertising.advertiser_account_reason(self.legacy), "email_unverified")


class NewSignUpTests(ManualPaymentTestCase):
    def sign_up(self, client, method, value):
        data = {"first_name": "Amina", "last_name": "Juma", "contact_method": method, "email": "", "phone_number": "",
                "username": "", "password1": NEW_PASSWORD, "password2": NEW_PASSWORD,
                {"email": "email", "phone": "phone_number", "username": "username"}[method]: value}
        return client.post(REGISTER, data)

    def test_every_method_gets_the_same_dashboard_now_and_after_signing_in_again(self):
        cases = (("email", "amina@example.com", "AMINA@example.com"), ("phone", "0712345678", "0712 345 678"),
                 ("username", "amina.j", "Amina.J"))
        pages = {}
        for method, value, typed in cases:
            with self.subTest(method):
                client = self.client_class()
                self.assertRedirects(self.sign_up(client, method, value), DASHBOARD, fetch_redirect_response=False)
                self.assertEqual(client.get(DASHBOARD).status_code, 200)
                client.post(LOGOUT)
                self.assertFalse(get_user(client).is_authenticated)
                again = self.client_class()
                response, visited = follow(again, LOGIN, "post", {"username": typed, "password": NEW_PASSWORD})
                self.assertEqual(response.request["PATH_INFO"], DASHBOARD)
                self.assertNotIn(VERIFY, visited)
                pages[method] = response.status_code
        self.assertEqual(set(pages.values()), {200})


class SignOutTests(ManualPaymentTestCase):
    """Sign out is a POST form with a CSRF token, in every authenticated navigation."""

    SIGN_OUT_FORM = re.compile(r'<form method="post" action="' + re.escape(LOGOUT) + r'"[^>]*>\s*<input type="hidden" '
                               r'name="csrfmiddlewaretoken"')

    def test_sign_out_is_in_the_navigation_of_every_layout(self):
        member = make_user("legacy@example.com", verified=False)
        self.client.force_login(member)
        pages = {"auth layout (old verification URL target)": PROFILE, "public layout (home)": reverse("home"),
                 "member layout (dashboard)": DASHBOARD, "plans": reverse("memberships:plans")}
        for label, url in pages.items():
            with self.subTest(label):
                page = self.client.get(url)
                self.assertEqual(page.status_code, 200)
                self.assertRegex(page.content.decode(), self.SIGN_OUT_FORM)

    def test_the_verification_page_offers_sign_out_and_a_way_back(self):
        staff = make_user("cm@example.com", role=User.Role.CONTENT_MANAGER, verified=False)
        self.client.force_login(staff)  # Staff keep the confirmation rule, so they can still land here.
        page = self.client.get(VERIFY)
        self.assertEqual(page.status_code, 200)
        self.assertRegex(page.content.decode(), self.SIGN_OUT_FORM)
        self.assertContains(page, f'href="{reverse("home")}"')
        self.assertContains(page, "Send a new link")

    def test_sign_out_is_post_only_and_csrf_protected(self):
        member = make_user("legacy@example.com", verified=False)
        enforcing = self.client_class(enforce_csrf_checks=True)
        enforcing.force_login(member)
        self.assertEqual(enforcing.post(LOGOUT).status_code, 403)  # No token: refused.
        self.assertTrue(get_user(enforcing).is_authenticated)
        page = enforcing.get(LOGOUT)  # GET only asks for confirmation.
        self.assertEqual(page.status_code, 200)
        self.assertTrue(get_user(enforcing).is_authenticated)
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()).group(1)
        enforcing.post(LOGOUT, {"csrfmiddlewaretoken": token})
        self.assertFalse(get_user(enforcing).is_authenticated)

    def test_the_mobile_menu_holds_the_same_sign_out_form(self):
        """Mobile uses the same collapsible #site-menu (toggle button), so the form must be inside it."""
        self.client.force_login(make_user("legacy@example.com", verified=False))
        html = self.client.get(reverse("home")).content.decode()
        menu = html.split('id="site-menu"', 1)[1].split("</nav>", 1)[0]
        self.assertRegex(menu, self.SIGN_OUT_FORM)
        self.assertIn('class="site-menu-toggle"', html)


class StaffRuleTests(ManualPaymentTestCase):
    def test_unconfirmed_staff_are_still_held_but_can_leave(self):
        staff = make_user("cm@example.com", role=User.Role.CONTENT_MANAGER, verified=False)
        self.client.force_login(staff)
        self.assertRedirects(self.client.get(DASHBOARD), VERIFY)
        response, visited = follow(self.client, DASHBOARD)
        self.assertEqual(response.request["PATH_INFO"], VERIFY)  # Ends there, no loop.
        self.client.post(LOGOUT)
        self.assertFalse(get_user(self.client).is_authenticated)

    def test_the_diagnostic_command_explains_both_cases_without_changing_anything(self):
        from io import StringIO

        from django.core.management import call_command

        make_user("legacy@example.com", verified=False)
        make_user("cm@example.com", role=User.Role.CONTENT_MANAGER, verified=False)
        member_out, staff_out = StringIO(), StringIO()
        call_command("explain_account_access", "LEGACY@example.com", stdout=member_out)
        call_command("explain_account_access", "cm@example.com", stdout=staff_out)
        self.assertIn("opens member pages: yes", member_out.getvalue())
        self.assertIn("Not held", member_out.getvalue())
        self.assertNotIn("legacy@example.com", member_out.getvalue())  # Identifier values are never printed.
        self.assertIn("HELD at /accounts/verify/required/", staff_out.getvalue())
        self.assertFalse(User.objects.get(email="legacy@example.com").email_verified)  # Read-only.

    def test_gate_redirects_are_never_cached(self):
        staff = make_user("cm@example.com", role=User.Role.CONTENT_MANAGER, verified=False)
        self.client.force_login(staff)
        response = self.client.get(DASHBOARD)
        self.assertIn("no-store", response.get("Cache-Control", ""))
        self.assertIn("no-store", self.client.get(VERIFY).get("Cache-Control", ""))
