"""Public homepage (GET /), restricted demo mode and the redirects around sign-in."""

from django.contrib.auth import get_user
from django.contrib.staticfiles import finders
from django.core import mail
from django.test import override_settings
from django.urls import resolve, reverse

from apps.accounts.tests.helpers import PASSWORD, AccountsTestCase, User, make_user
from apps.administration.admin import SiteConfigurationForm
from apps.audit.models import AuditEvent
from apps.core import features, services
from apps.core.models import SiteConfiguration
from apps.core.views import HomeView

HOME = reverse("home")
LOGIN = reverse("accounts:login")
REGISTER = reverse("accounts:register")
PROFILE = reverse("accounts:profile")
DASHBOARD = reverse("dashboard")
CONSOLE = reverse("administration:overview")

# Wording the homepage must never use while financial operations are off.
EARNINGS_CLAIMS = ("earn money", "get paid", "cash out", "guaranteed", "withdraw your", "instant payout")


class HomeTestCase(AccountsTestCase):
    def setUp(self):
        super().setUp()
        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)

    def configure(self, **changes):
        version = SiteConfiguration.objects.get().version
        services.update_site_configuration(self.root, expected_version=version, changes=changes)


@override_settings(RESTRICTED_DEMO_MODE=False)
class HomepageTests(HomeTestCase):
    def test_root_url_is_the_homepage(self):
        self.assertEqual(HOME, "/")
        self.assertIs(resolve("/").func.view_class, HomeView)
        response = self.client.get(HOME)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "public/home.html")
        self.assertTemplateUsed(response, "base.html")

    def test_branding_and_explanation(self):
        response = self.client.get(HOME)
        self.assertContains(response, '<h1 id="home-title" class="hero__title">ClipCash</h1>', html=True)
        self.assertContains(response, "How it works")
        self.assertContains(response, "Verified viewing")

    def test_configured_name_and_description_are_used_and_escaped(self):
        self.configure(platform_name="Clip Demo", platform_description="Watch <b>reviewed</b> clips.")
        response = self.client.get(HOME)
        self.assertContains(response, "Clip Demo")
        self.assertContains(response, "Watch &lt;b&gt;reviewed&lt;/b&gt; clips.")
        self.assertNotContains(response, "<b>reviewed</b>")

    def test_no_earnings_promises_and_financial_notice_shown(self):
        response = self.client.get(HOME)
        self.assertTrue(response.context["financial_locked"])
        self.assertContains(response, "No money is paid out on this site.")
        text = response.content.decode().lower()
        for phrase in EARNINGS_CLAIMS:
            self.assertNotIn(phrase, text, phrase)

    def test_anonymous_links(self):
        response = self.client.get(HOME)
        self.assertContains(response, f'href="{LOGIN}"')
        self.assertContains(response, f'href="{reverse("accounts:password_reset")}"')
        self.assertContains(response, f'href="{REGISTER}"')  # Registration open here.
        self.assertNotContains(response, f'href="{DASHBOARD}"')
        self.assertNotContains(response, f'href="{CONSOLE}"')

    def test_registration_closed_by_configuration_hides_links(self):
        self.configure(registration_enabled=False)
        response = self.client.get(HOME)
        self.assertNotContains(response, REGISTER)
        self.assertContains(response, "New accounts are not open to the public")
        self.assertEqual(self.client.get(REGISTER).status_code, 403)

    def test_member_links(self):
        member = make_user()
        self.client.force_login(member)
        response = self.client.get(HOME)
        self.assertEqual(response.status_code, 200)  # Shown, not redirected.
        self.assertContains(response, f'href="{DASHBOARD}"')
        self.assertContains(response, member.email)
        self.assertNotContains(response, f'href="{CONSOLE}"')
        self.assertNotContains(response, REGISTER)
        self.assertNotContains(response, f'href="{LOGIN}"')

    def test_staff_get_the_console_link(self):
        self.client.force_login(make_user("cm@example.com", role=User.Role.CONTENT_MANAGER))
        self.assertContains(self.client.get(HOME), f'href="{CONSOLE}"')

    def test_external_css_only(self):
        response = self.client.get(HOME)
        self.assertContains(response, '<link rel="stylesheet" href="/static/css/clipcash.css">', html=True)
        content = response.content.decode()
        self.assertNotIn("<style", content)
        self.assertNotIn(" style=", content)
        self.assertIn("style-src 'self'", response["Content-Security-Policy"])
        self.assertIsNotNone(finders.find("css/clipcash.css"))

    def test_brand_links_home_for_visitors(self):
        self.assertContains(self.client.get(LOGIN), f'<a class="brand" href="{HOME}">', count=1)

    def test_visiting_records_no_audit_events(self):
        before = AuditEvent.objects.count()
        self.client.get(HOME)
        self.client.force_login(make_user())
        self.client.get(HOME)
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_maintenance_mode_applies(self):
        self.configure(maintenance_mode=True)
        self.assertEqual(self.client.get(HOME).status_code, 503)

    def test_get_only_content(self):
        self.assertEqual(self.client.post(HOME).status_code, 405)


@override_settings(RESTRICTED_DEMO_MODE=True)
class RestrictedDemoTests(HomeTestCase):
    def test_registration_off_whatever_the_stored_value(self):
        self.assertTrue(SiteConfiguration.objects.get().registration_enabled)  # Stored value says open.
        self.assertFalse(features.is_enabled("registration"))
        self.assertFalse(features.feature_states()["registration"])

        class Spoofed:
            registration_enabled = True

        self.assertFalse(features.is_enabled("registration", config=Spoofed()))

    def test_other_flags_unchanged(self):
        self.configure(videos_enabled=True)
        self.assertTrue(features.is_enabled("videos"))
        for key in features.FINANCIAL_FEATURES:
            self.assertFalse(features.is_enabled(key))

    def test_homepage_and_login_hide_registration(self):
        response = self.client.get(HOME)
        self.assertContains(response, "Restricted demonstration")
        self.assertContains(response, "New accounts are not open to the public")
        self.assertContains(response, f'href="{LOGIN}"')
        self.assertNotContains(response, REGISTER)
        self.assertNotContains(self.client.get(LOGIN), REGISTER)

    def test_registration_refused_server_side(self):
        response = self.client.get(REGISTER)
        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, "accounts/registration_closed.html")
        response = self.client.post(REGISTER, {"email": "new@example.com"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(email="new@example.com").exists())
        self.assertEqual(mail.outbox, [])
        self.assertFalse(AuditEvent.objects.filter(action__startswith="account.registration").exists())

    def test_existing_accounts_can_still_sign_in(self):
        make_user()
        response = self.client.post(LOGIN, {"username": "member@example.com", "password": PASSWORD})
        self.assertRedirects(response, PROFILE)
        self.assertTrue(AuditEvent.objects.filter(action="auth.login.succeeded").exists())

    def test_admin_explains_the_override(self):
        form = SiteConfigurationForm(instance=SiteConfiguration.objects.get())
        self.assertIn("DJANGO_RESTRICTED_DEMO", str(form.fields["registration_enabled"].help_text))


class AuthenticationRedirectTests(HomeTestCase):
    def test_protected_pages_send_visitors_to_sign_in(self):
        for url in (DASHBOARD, PROFILE, reverse("activity")):
            with self.subTest(url=url):
                self.assertRedirects(self.client.get(url), f"{LOGIN}?next={url}", fetch_redirect_response=False)

    def test_console_stays_staff_only(self):
        response = self.client.get(CONSOLE)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(reverse("admin:login")))
        self.client.force_login(make_user())
        response = self.client.get(CONSOLE)
        self.assertEqual(response.status_code, 302)  # Members are not let in.
        self.assertTrue(response["Location"].startswith(reverse("admin:login")))

    def test_sign_in_returns_to_the_homepage_when_asked(self):
        make_user()
        response = self.client.post(f"{LOGIN}?next={HOME}",
                                    {"username": "member@example.com", "password": PASSWORD, "next": HOME})
        self.assertRedirects(response, HOME)

    def test_signed_in_users_skip_the_login_page(self):
        self.client.force_login(make_user())
        self.assertRedirects(self.client.get(LOGIN), PROFILE)

    def test_unverified_member_sees_homepage_but_not_dashboard(self):
        self.client.force_login(make_user(verified=False))
        self.assertEqual(self.client.get(HOME).status_code, 200)
        self.assertRedirects(self.client.get(DASHBOARD), reverse("accounts:verification_required"))

    def test_sign_out_is_audited_and_lands_on_sign_in(self):
        self.client.force_login(make_user())
        self.assertRedirects(self.client.post(reverse("accounts:logout")), LOGIN)
        self.assertFalse(get_user(self.client).is_authenticated)
        self.assertTrue(AuditEvent.objects.filter(action="auth.logout").exists())
        self.assertEqual(self.client.get(HOME).status_code, 200)


@override_settings(DEBUG=False, ALLOWED_HOSTS=["clipcash.example"], RESTRICTED_DEMO_MODE=True)
class ProductionHomepageTests(HomeTestCase):
    def test_homepage_and_custom_404_without_debug(self):
        response = self.client.get(HOME, HTTP_HOST="clipcash.example")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, REGISTER)
        response = self.client.get("/no-such-page/", HTTP_HOST="clipcash.example")
        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "404.html")
        self.assertNotContains(response, "URLconf", status_code=404)  # Not Django's debug page.

    def test_unknown_host_rejected(self):
        self.assertEqual(self.client.get(HOME, HTTP_HOST="attacker.example").status_code, 400)

    @override_settings(SECURE_SSL_REDIRECT=True)
    def test_https_redirect(self):
        response = self.client.get(HOME, HTTP_HOST="clipcash.example")
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "https://clipcash.example/")
        self.assertEqual(self.client.get(HOME, HTTP_HOST="clipcash.example", secure=True).status_code, 200)
