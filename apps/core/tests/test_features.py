from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.http import Http404, HttpResponse
from django.test import RequestFactory, TestCase
from django.urls import reverse

from apps.core import features, services
from apps.core.models import SiteConfiguration

User = get_user_model()
PASSWORD = "Correct-Horse-Battery-9"


class FeatureFlagTests(TestCase):
    def setUp(self):
        cache.clear()
        self.root = User.objects.create_superuser("root@example.com", PASSWORD)

    def set_flags(self, **changes):
        version = SiteConfiguration.objects.get().version
        services.update_site_configuration(self.root, expected_version=version, changes=changes)

    def test_explicit_defaults(self):
        self.assertEqual(
            features.feature_states(),
            {
                "registration": True,
                "memberships": False,
                "videos": False,
                "advertisers": False,
                "real_money_rewards": False,
                "live_payments": False,
                "withdrawals": False,
            },
        )
        for key, feature in features.FEATURES.items():
            self.assertEqual(features.is_enabled(key), feature.default, key)

    def test_business_flags_follow_configuration(self):
        self.set_flags(videos_enabled=True, registration_enabled=False)
        self.assertTrue(features.is_enabled("videos"))
        self.assertFalse(features.is_enabled("registration"))

    def test_financial_flags_locked_off(self):
        self.assertEqual(features.FINANCIAL_FEATURES, {"real_money_rewards", "live_payments", "withdrawals"})
        for key in features.FINANCIAL_FEATURES:
            feature = features.get_feature(key)
            self.assertIsNone(feature.config_field)
            self.assertFalse(features.is_enabled(key))

    def test_financial_flags_ignore_any_stored_value(self):
        class Spoofed:
            real_money_rewards = True
            live_payments = True
            withdrawals = True

        for key in features.FINANCIAL_FEATURES:
            self.assertFalse(features.is_enabled(key, config=Spoofed()))

    def test_financial_flags_cannot_be_set_through_service(self):
        for field in ("real_money_rewards", "real_money_rewards_enabled", "live_payments_enabled",
                      "withdrawals_enabled"):
            with self.subTest(field=field), self.assertRaises(Exception):
                self.set_flags(**{field: True})

    def test_unknown_feature_is_an_error(self):
        with self.assertRaises(features.UnknownFeatureError):
            features.is_enabled("payouts")
        with self.assertRaises(features.UnknownFeatureError):
            features.feature_required("nope")

    def test_non_boolean_value_falls_back_to_default(self):
        class Broken:
            registration_enabled = "yes"

        self.assertTrue(features.is_enabled("registration", config=Broken()))
        self.assertFalse(features.is_enabled("videos", config=Broken()))

    def test_missing_configuration_uses_defaults(self):
        SiteConfiguration.objects.all().delete()
        cache.clear()
        with self.assertLogs("clipcash.security", "ERROR"):
            self.assertTrue(features.is_enabled("registration"))

    def test_feature_required_decorator(self):
        view = features.feature_required("videos")(lambda request: HttpResponse("ok"))
        request = RequestFactory().get("/")
        with self.assertRaises(Http404):
            view(request)
        self.set_flags(videos_enabled=True)
        self.assertEqual(view(request).status_code, 200)


class RegistrationFlagTests(TestCase):
    def setUp(self):
        cache.clear()
        root = User.objects.create_superuser("root@example.com", PASSWORD)
        services.update_site_configuration(root, expected_version=1, changes={"registration_enabled": False})

    def test_registration_closed_on_get_and_post(self):
        url = reverse("accounts:register")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, "accounts/registration_closed.html")
        self.client.post(url, {"email": "new@example.com"})
        self.assertFalse(User.objects.filter(email="new@example.com").exists())

    def test_register_link_hidden(self):
        self.assertNotContains(self.client.get(reverse("accounts:login")), reverse("accounts:register"))
