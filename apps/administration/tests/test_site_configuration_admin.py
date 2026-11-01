from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from apps.core.models import SITE_CONFIGURATION_ID, SiteConfiguration

User = get_user_model()
PASSWORD = "Correct-Horse-Battery-9"
CHANGE_URL = reverse("admin:core_siteconfiguration_change", args=[SITE_CONFIGURATION_ID])


def form_data(config, **overrides):
    data = {
        "platform_name": config.platform_name,
        "platform_description": config.platform_description,
        "website_url": config.website_url,
        "company_name": config.company_name,
        "company_address": config.company_address,
        "support_email": config.support_email,
        "support_phone": config.support_phone,
        "default_timezone": config.default_timezone,
        "default_currency": config.default_currency,
        "default_language": config.default_language,
        "maintenance_message": config.maintenance_message,
        "expected_version": config.version,
        "_save": "Save",
    }
    for flag in ("maintenance_mode", "registration_enabled", "memberships_enabled",
                 "videos_enabled", "advertisers_enabled"):
        if getattr(config, flag):
            data[flag] = "on"
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


class SiteConfigurationAdminTests(TestCase):
    def setUp(self):
        cache.clear()
        self.root = User.objects.create_superuser("root@example.com", PASSWORD)
        self.cm = User.objects.create_user(
            "cm@example.com", PASSWORD, role=User.Role.CONTENT_MANAGER, status=User.Status.ACTIVE
        )
        # Even with every model permission granted, a non-superuser is refused.
        self.cm.user_permissions.add(*Permission.objects.filter(content_type__app_label="core"))

    def config(self):
        return SiteConfiguration.objects.get()

    def test_super_admin_can_edit(self):
        self.client.force_login(self.root)
        self.assertEqual(self.client.get(CHANGE_URL).status_code, 200)
        response = self.client.post(CHANGE_URL, form_data(self.config(), platform_name="ClipCash TZ",
                                                          support_phone="0712345678"))
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and response.context["adminform"].form.errors)
        config = self.config()
        self.assertEqual((config.platform_name, config.support_phone, config.version),
                         ("ClipCash TZ", "+255712345678", 2))
        self.assertEqual(config.updated_by, self.root)

    def test_content_manager_refused_even_with_permissions(self):
        self.client.force_login(self.cm)
        self.assertEqual(self.client.get(CHANGE_URL).status_code, 403)
        self.assertEqual(self.client.post(CHANGE_URL, form_data(self.config(), maintenance_mode="on")).status_code, 403)
        self.assertFalse(self.config().maintenance_mode)
        index = self.client.get(reverse("admin:index"))
        self.assertNotContains(index, "Site configuration")

    def test_members_and_anonymous_refused(self):
        response = self.client.get(CHANGE_URL)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response["Location"])

    def test_invalid_values_rejected_server_side(self):
        self.client.force_login(self.root)
        for field, value in (("support_phone", "12345"), ("website_url", "http://insecure.example"),
                             ("default_timezone", "Nowhere/City"), ("default_currency", "USD"),
                             ("default_language", "fr"), ("platform_name", "  ")):
            with self.subTest(field=field):
                response = self.client.post(CHANGE_URL, form_data(self.config(), **{field: value}))
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context["adminform"].form.errors)
        self.assertEqual(self.config().version, 1)

    def test_stale_form_rejected(self):
        self.client.force_login(self.root)
        stale = form_data(self.config(), platform_name="Mine")
        self.client.post(CHANGE_URL, form_data(self.config(), platform_name="Theirs"))
        response = self.client.post(CHANGE_URL, stale)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "changed by someone else")
        self.assertEqual(self.config().platform_name, "Theirs")

    def test_protected_fields_cannot_be_posted(self):
        self.client.force_login(self.root)
        self.client.post(CHANGE_URL, form_data(self.config(), version=999, updated_by=self.cm.pk,
                                               real_money_rewards="on", withdrawals_enabled="on"))
        config = self.config()
        # Only unknown/protected fields were posted, so nothing changed.
        self.assertEqual(config.version, 1)
        self.assertIsNone(config.updated_by_id)

    def test_no_add_or_delete(self):
        self.client.force_login(self.root)
        self.assertEqual(self.client.get(reverse("admin:core_siteconfiguration_add")).status_code, 403)
        delete_url = reverse("admin:core_siteconfiguration_delete", args=[SITE_CONFIGURATION_ID])
        self.assertEqual(self.client.post(delete_url, {"post": "yes"}).status_code, 403)
        self.assertTrue(SiteConfiguration.objects.exists())

    def test_changelist_redirects_to_single_row(self):
        self.client.force_login(self.root)
        response = self.client.get(reverse("admin:core_siteconfiguration_changelist"))
        self.assertRedirects(response, CHANGE_URL)

    def test_financial_features_shown_locked(self):
        self.client.force_login(self.root)
        self.assertContains(self.client.get(CHANGE_URL), "Locked off")
