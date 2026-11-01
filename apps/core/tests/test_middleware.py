from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone, translation

from apps.core import services
from apps.core.models import SiteConfiguration

User = get_user_model()
PASSWORD = "Correct-Horse-Battery-9"


class MiddlewareTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.root = User.objects.create_superuser("root@example.com", PASSWORD)

    def configure(self, **changes):
        version = SiteConfiguration.objects.get().version
        services.update_site_configuration(self.root, expected_version=version, changes=changes)


class MaintenanceModeTests(MiddlewareTestCase):
    def setUp(self):
        super().setUp()
        self.configure(maintenance_mode=True, maintenance_message="Back at 18:00.")

    def test_visitors_and_members_get_503(self):
        member = User.objects.create_user("m@example.com", PASSWORD, status=User.Status.ACTIVE)
        for client_user in (None, member):
            with self.subTest(user=client_user):
                if client_user:
                    self.client.force_login(client_user)
                response = self.client.get(reverse("accounts:register"))
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response["Retry-After"], "600")
                self.assertContains(response, "Back at 18:00.", status_code=503)

    def test_staff_pass_through(self):
        self.client.force_login(self.root)
        self.assertEqual(self.client.get(reverse("accounts:profile")).status_code, 302)  # unverified -> verify page
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)

    def test_sign_in_remains_reachable(self):
        self.assertEqual(self.client.get(reverse("accounts:login")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:login")).status_code, 200)

    def test_off_by_default(self):
        self.configure(maintenance_mode=False)
        self.assertEqual(self.client.get(reverse("accounts:register")).status_code, 200)


class RegionalDefaultsTests(MiddlewareTestCase):
    def test_timezone_activated_for_request(self):
        seen = {}

        def capture(request):
            seen["tz"] = timezone.get_current_timezone_name()
            from django.http import HttpResponse

            return HttpResponse()

        from apps.core.middleware import SiteConfigurationMiddleware
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        SiteConfigurationMiddleware(capture)(request)
        self.assertEqual(seen["tz"], "Africa/Dar_es_Salaam")

    def test_default_language_applies_without_browser_preference(self):
        self.configure(default_language="sw")
        response = self.client.get(reverse("accounts:login"))
        self.assertEqual(response.headers.get("Content-Language"), "sw")
        translation.activate("en")

    def test_browser_preference_wins(self):
        self.configure(default_language="sw")
        response = self.client.get(reverse("accounts:login"), HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(response.headers.get("Content-Language"), "en")

    def test_branding_from_configuration(self):
        self.configure(platform_name="ClipCash Tanzania", support_email="help@clipcash.example")
        response = self.client.get(reverse("accounts:login"))
        self.assertContains(response, "ClipCash Tanzania")
        self.assertContains(response, "mailto:help@clipcash.example")
