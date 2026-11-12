"""
REG-01: public registration, transactional email and launch-facing copy.

The whole new-member journey runs through the browser-facing views, with emails
delivered through the durable outbox: register, open the emailed link, choose a
password, sign in, land on the dashboard with a clear next step; resend and
password recovery; invalid, expired and reused links; duplicate registration.
Emailed links use DJANGO_PUBLIC_BASE_URL (the site's HTTPS origin), never the
Host header of the request that asked for them.
"""

import re
from urllib.parse import urlsplit

from django.core import mail
from django.test import override_settings
from django.urls import reverse

from apps.memberships.tests.helpers import enable_memberships

from .helpers import NEW_PASSWORD, PASSWORD, AccountsTestCase, User, links_in_last_email, make_user

HOME = reverse("home")
LOGIN = reverse("accounts:login")
REGISTER = reverse("accounts:register")
DASHBOARD = reverse("dashboard")
CONSOLE = reverse("administration:overview")
RESET = reverse("accounts:password_reset")
RESEND = reverse("accounts:resend_verification")

ORIGIN = "https://clipcash.example"
PUBLIC = override_settings(RESTRICTED_DEMO_MODE=False, PUBLIC_REGISTRATION_APPROVED=True,
                           PUBLIC_BASE_URL=ORIGIN, ALLOWED_HOSTS=["testserver", "clipcash.example",
                                                                  "other.clipcash.example"])
CLOSED = override_settings(RESTRICTED_DEMO_MODE=False, PUBLIC_REGISTRATION_APPROVED=False)
DEMO = override_settings(RESTRICTED_DEMO_MODE=True, PUBLIC_REGISTRATION_APPROVED=True)

# Wording that must not appear on public-mode screens (demo disclosures belong to the demo).
DEMO_WORDING = ("demonstration", "demo rewards", "sandbox", "restricted demo")


def emailed_link(fragment):
    (link,) = [link for link in links_in_last_email() if fragment in link]
    return link


def path_of(link):
    parts = urlsplit(link)
    return parts.path


class EntryPointTests(AccountsTestCase):
    """Every Create account entry point opens the real form, and the server applies the same policy."""

    def register_links(self, response):
        return re.findall(r'<a [^>]*href="%s"[^>]*>([^<]+)</a>' % re.escape(REGISTER), response.content.decode())

    @PUBLIC
    def test_open_registration_shows_create_account_everywhere(self):
        home = self.client.get(HOME)
        labels = self.register_links(home)
        self.assertIn("Create account", labels)  # Hero button and menu.
        self.assertGreaterEqual(labels.count("Create account"), 2)
        self.assertContains(home, 'class="public-register"')  # The menu entry (also the mobile menu).
        self.assertIn("Create account", self.register_links(self.client.get(LOGIN)))
        form = self.client.get(REGISTER)
        self.assertEqual(form.status_code, 200)
        self.assertTemplateUsed(form, "accounts/register.html")
        self.assertContains(form, 'name="phone_number"')

    def test_closed_or_demo_registration_hides_links_and_refuses_the_form(self):
        for name, mode in (("closed", CLOSED), ("demo", DEMO)):
            with self.subTest(name), mode:
                self.assertEqual(self.register_links(self.client.get(HOME)), [])
                self.assertEqual(self.register_links(self.client.get(LOGIN)), [])
                self.assertEqual(self.client.get(REGISTER).status_code, 403)
                response = self.client.post(REGISTER, {"phone_number": "0712345678", "password1": NEW_PASSWORD,
                                                       "password2": NEW_PASSWORD})
                self.assertEqual(response.status_code, 403)
                self.assertFalse(User.objects.filter(phone_number="+255712345678").exists())


@PUBLIC
class NewMemberJourneyTests(AccountsTestCase):
    def setUp(self):
        super().setUp()
        enable_memberships()

    def register(self, email="asha@example.com", **extra):
        """REG-02: sign-up is phone-first; an email account still pending (from before, or
        made by staff) gets its activation link from the resend form."""
        mail.outbox.clear()
        User.objects.create_user(email, None, status=User.Status.PENDING)
        response = self.client.post(RESEND, {"email": email}, **extra)
        self.assertRedirects(response, reverse("accounts:resend_verification_done"))
        return response

    def activate(self, link, password=NEW_PASSWORD):
        response = self.client.get(path_of(link), follow=True, secure=True)
        set_password = response.redirect_chain[-1][0]
        return self.client.post(set_password, {"new_password1": password, "new_password2": password}, secure=True)

    def test_register_verify_sign_in_and_reach_the_dashboard_next_step(self):
        self.register()
        email = mail.outbox[-1]
        self.assertEqual(email.to, ["asha@example.com"])
        link = emailed_link("/accounts/activate/")
        self.assertTrue(link.startswith(ORIGIN + "/accounts/activate/"), link)

        response = self.activate(link)
        self.assertRedirects(response, reverse("accounts:activation_complete"))
        complete = self.client.get(reverse("accounts:activation_complete"))
        self.assertContains(complete, "choose a membership plan")
        user = User.objects.get(email="asha@example.com")
        self.assertEqual((user.status, user.email_verified), (User.Status.ACTIVE, True))

        response = self.client.post(LOGIN, {"username": "asha@example.com", "password": NEW_PASSWORD})
        self.assertRedirects(response, DASHBOARD)
        dashboard = self.client.get(DASHBOARD)
        self.assertContains(dashboard, "Next step: choose a membership")
        self.assertContains(dashboard, f'href="{reverse("memberships:plans")}"')
        self.assertContains(dashboard, "stays pending until it is activated")
        for phrase in ("Demo rewards and sandbox payments",):
            self.assertNotContains(dashboard, phrase)

    def test_links_use_the_configured_origin_not_the_request_host(self):
        self.register(HTTP_HOST="other.clipcash.example")  # An allowed host over plain HTTP.
        self.assertTrue(emailed_link("/accounts/activate/").startswith(ORIGIN + "/"))
        make_user("member@example.com")
        mail.outbox.clear()
        self.client.post(RESET, {"email": "member@example.com"}, HTTP_HOST="other.clipcash.example")
        self.assertTrue(emailed_link("/accounts/reset/").startswith(ORIGIN + "/accounts/reset/"))

    @override_settings(PUBLIC_BASE_URL="")
    def test_without_a_configured_origin_links_follow_the_validated_request(self):
        self.register()
        self.assertTrue(emailed_link("/accounts/activate/").startswith("http://testserver/"))

    def test_resend_sends_a_fresh_link_without_revealing_accounts(self):
        self.register()
        first = emailed_link("/accounts/activate/")
        mail.outbox.clear()
        self.assertRedirects(self.client.post(RESEND, {"email": "asha@example.com"}),
                             reverse("accounts:resend_verification_done"))
        self.assertEqual(len(mail.outbox), 1)
        second = emailed_link("/accounts/activate/")
        mail.outbox.clear()
        unknown = self.client.post(RESEND, {"email": "nobody@example.com"})
        self.assertRedirects(unknown, reverse("accounts:resend_verification_done"))
        self.assertEqual(mail.outbox, [])
        self.assertRedirects(self.activate(second), reverse("accounts:activation_complete"))
        # The earlier link and the used link no longer work.
        for link in (first, second):
            response = self.client.get(path_of(link), follow=True, secure=True)
            self.assertFalse(response.context["validlink"])

    def test_invalid_and_expired_activation_links(self):
        self.register()
        link = emailed_link("/accounts/activate/")
        tampered = re.sub(r"/[^/]+/$", "/bogus-token/", path_of(link))
        self.assertFalse(self.client.get(tampered, follow=True).context["validlink"])
        with override_settings(ACCOUNTS_EMAIL_VERIFICATION_TIMEOUT=-1):
            self.assertFalse(self.client.get(path_of(link), follow=True).context["validlink"])
        self.assertEqual(User.objects.get(email="asha@example.com").status, User.Status.PENDING)

    def test_password_recovery_through_the_browser(self):
        make_user("member@example.com")
        mail.outbox.clear()
        done = reverse("accounts:password_reset_done")
        self.assertRedirects(self.client.post(RESET, {"email": "member@example.com"}), done)
        link = emailed_link("/accounts/reset/")
        # Unknown and not-yet-activated addresses get the same page and no email.
        sent = len(mail.outbox)
        self.register("pending@example.com")
        mail.outbox.clear()
        for address in ("nobody@example.com", "pending@example.com"):
            self.assertRedirects(self.client.post(RESET, {"email": address}), done)
        self.assertEqual(mail.outbox, [])
        self.assertEqual(sent, 1)

        response = self.client.get(path_of(link), follow=True, secure=True)
        self.assertTrue(response.context["validlink"])
        set_password = response.redirect_chain[-1][0]
        response = self.client.post(set_password, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
                                    secure=True)
        self.assertRedirects(response, reverse("accounts:password_reset_complete"))
        # Reused link: invalid. Old password: refused. New password: signs in to the dashboard.
        self.assertFalse(self.client.get(path_of(link), follow=True, secure=True).context["validlink"])
        self.assertEqual(self.client.post(LOGIN, {"username": "member@example.com",
                                                  "password": PASSWORD}).status_code, 200)
        self.assertRedirects(self.client.post(LOGIN, {"username": "member@example.com", "password": NEW_PASSWORD}),
                             DASHBOARD)


@PUBLIC
class BrowserOriginTests(AccountsTestCase):
    """Real browsers send an Origin header with form POSTs, and CSRF is enforced. Under the
    former "no-referrer" policy that header was "null" and every emailed-link form failed."""

    def browser(self):
        return self.client_class(enforce_csrf_checks=True)

    def post_form(self, client, url, data):
        page = client.get(url)
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()).group(1)
        return client.post(url, {**data, "csrfmiddlewaretoken": token}, HTTP_ORIGIN="http://testserver",
                           HTTP_REFERER="http://testserver/")

    def test_emailed_link_pages_send_only_the_origin(self):
        browser = self.browser()
        User.objects.create_user("asha@example.com", None, status=User.Status.PENDING)
        self.post_form(browser, RESEND, {"email": "asha@example.com"})
        opened = browser.get(path_of(emailed_link("/accounts/activate/")))
        self.assertEqual(opened["Referrer-Policy"], "strict-origin")
        self.assertNotEqual(opened["Referrer-Policy"], "no-referrer")  # Would make browsers send Origin: null.
        response = self.post_form(browser, opened.url, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
        self.assertRedirects(response, reverse("accounts:activation_complete"))

        mail.outbox.clear()
        self.post_form(browser, RESET, {"email": "asha@example.com"})
        opened = browser.get(path_of(emailed_link("/accounts/reset/")))
        response = self.post_form(browser, opened.url, {"new_password1": PASSWORD, "new_password2": PASSWORD})
        self.assertRedirects(response, reverse("accounts:password_reset_complete"))

    def test_origin_null_is_refused(self):
        browser = self.browser()
        page = browser.get(REGISTER)
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()).group(1)
        response = browser.post(REGISTER, {"phone_number": "0712345678", "password1": NEW_PASSWORD,
                                           "password2": NEW_PASSWORD, "csrfmiddlewaretoken": token},
                                HTTP_ORIGIN="null")
        self.assertEqual(response.status_code, 403)


@PUBLIC
class SignInAndStaffAccessTests(AccountsTestCase):
    def test_administrator_signs_in_and_reaches_the_console(self):
        make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.assertRedirects(self.client.post(LOGIN, {"username": "root@example.com", "password": PASSWORD}),
                             DASHBOARD)
        self.assertEqual(self.client.get(CONSOLE).status_code, 200)

    def test_regular_member_is_denied_staff_pages(self):
        make_user("member@example.com")
        self.client.post(LOGIN, {"username": "member@example.com", "password": PASSWORD})
        for url in (CONSOLE, reverse("administration:membership_payments"), reverse("admin:index")):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertIn(response.status_code, (302, 403))
                if response.status_code == 302:
                    self.assertTrue(response["Location"].startswith(reverse("admin:login")), response["Location"])


class PublicCopyTests(AccountsTestCase):
    """Public mode describes what customers can do; the restricted demo keeps its disclosures."""

    def visible_text(self, response):
        html = response.content.decode()
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S)
        return re.sub(r"<[^>]+>", " ", html).lower()

    def public_pages(self):
        return (HOME, LOGIN, REGISTER, RESET, RESEND, reverse("accounts:register_done"),
                reverse("accounts:activation_complete"), reverse("accounts:password_reset_done"))

    @PUBLIC
    def test_public_mode_has_no_demo_wording(self):
        for url in self.public_pages():
            with self.subTest(url=url):
                text = self.visible_text(self.client.get(url))
                for phrase in DEMO_WORDING:
                    self.assertNotIn(phrase, text, phrase)
                self.assertIn("cash rewards and withdrawals are not available", text)

    @PUBLIC
    def test_public_homepage_explains_pending_plans_and_payment_state(self):
        text = self.visible_text(self.client.get(HOME))
        self.assertIn("stays pending until it is activated", text)
        self.assertIn("membership payments are not open yet", text)
        self.assertNotIn("airtel", text)
        with override_settings(MANUAL_MEMBERSHIP_PAYMENTS_ENABLED=True):
            text = self.visible_text(self.client.get(HOME))
        self.assertIn("airtel money (lipa)", text)
        self.assertNotIn("membership payments are not open yet", text)
        self.assertIn("withdrawals and payouts are not available", text)

    @DEMO
    def test_restricted_demo_keeps_its_disclosures(self):
        home = self.visible_text(self.client.get(HOME))
        for phrase in ("restricted demonstration", "no money is paid out on this site",
                       "demonstration rewards are not wallet money", "sandbox payments do not activate access"):
            self.assertIn(phrase, home)
        self.assertIn("demo rewards have no cash value", self.visible_text(self.client.get(LOGIN)))
        closed = self.client.get(REGISTER)
        self.assertEqual(closed.status_code, 403)
        self.assertIn("restricted demonstration", self.visible_text(closed))
