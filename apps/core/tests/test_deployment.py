"""
Production deployment configuration (docs/deployment/render.md).

Settings are evaluated once per process, so production-like configurations run
in a subprocess with a clean, nonsecret environment. Nothing here connects to
a remote database.
"""

import json
import logging
import os
import subprocess
import sys
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.db import OperationalError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.core import services
from apps.core.log_filters import RedactTokenPaths
from apps.core.models import SiteConfiguration
from apps.core.tests.test_middleware import PASSWORD, User

# Fake values only. The reserved .invalid domain can never resolve, so no
# command run here can reach a real server.
PRODUCTION_ENV = {
    "DJANGO_DEBUG": "False",
    "DJANGO_SECRET_KEY": "deployment-test-value-not-a-real-secret-0123456789abcdefghij",
    "DATABASE_URL": "postgres://clipcash:example-password@clipcash-db.invalid:5432/clipcash",
    "DJANGO_DB_SSLMODE": "require",
    "RENDER_EXTERNAL_HOSTNAME": "clipcash-example.onrender.com",
    "DJANGO_CACHE_URL": "dbcache://clipcash_cache",
    "DJANGO_SECURE_SSL_REDIRECT": "True",
    "DJANGO_SECURE_PROXY_SSL_HEADER": "True",
}
ENV_PREFIXES = ("DJANGO_", "DB_", "DATABASE_URL", "RENDER", "VIDEO_", "BUNNY_")

SETTINGS_PROBE = """
import json, django
django.setup()
from django.conf import settings as s
from apps.core.features import FINANCIAL_FEATURES, is_enabled
from apps.payments.providers import sandbox_active
from django.urls import resolve
db = s.DATABASES["default"]
print(json.dumps({
    "DEBUG": s.DEBUG,
    "ALLOWED_HOSTS": s.ALLOWED_HOSTS,
    "CSRF_TRUSTED_ORIGINS": s.CSRF_TRUSTED_ORIGINS,
    "SECURE_PROXY_SSL_HEADER": getattr(s, "SECURE_PROXY_SSL_HEADER", None),
    "SECURE_SSL_REDIRECT": s.SECURE_SSL_REDIRECT,
    "SECURE_HSTS_SECONDS": s.SECURE_HSTS_SECONDS,
    "SESSION_COOKIE_SECURE": s.SESSION_COOKIE_SECURE,
    "CSRF_COOKIE_SECURE": s.CSRF_COOKIE_SECURE,
    "db": {k: db.get(k) for k in ("ENGINE", "NAME", "HOST", "PORT", "OPTIONS", "CONN_MAX_AGE")},
    "TEST_NAME": db["TEST"]["NAME"],
    "STATICFILES_BACKEND": s.STORAGES["staticfiles"]["BACKEND"],
    "THROTTLE_BACKEND": s.ACCOUNTS_THROTTLE_BACKEND,
    "PAYMENTS_MODE": s.PAYMENTS_MODE,
    "sandbox_active": sandbox_active(),
    "financial_enabled": sorted(k for k in FINANCIAL_FEATURES if is_enabled(k)),
    "VIDEO_PROVIDER": s.VIDEO_PROVIDER,
    "RESTRICTED_DEMO_MODE": s.RESTRICTED_DEMO_MODE,
    # Decided before any configuration lookup, so no database is needed here.
    "registration_enabled": is_enabled("registration") if s.RESTRICTED_DEMO_MODE else None,
    "home_view": resolve("/").view_name,
    "settings_mention_url": any("example-password" in repr(getattr(s, n)) for n in dir(s)
                                if n.isupper() and n != "DATABASES"),
}))
"""


def run_python(args, **overrides):
    env = {key: value for key, value in os.environ.items() if not key.startswith(ENV_PREFIXES)}
    env.update(PRODUCTION_ENV, DJANGO_SETTINGS_MODULE="config.settings", **overrides)
    env = {key: value for key, value in env.items() if value is not None}
    return subprocess.run([sys.executable, *args], cwd=settings.BASE_DIR, env=env, capture_output=True,
                          text=True, timeout=120)


def production_settings(**overrides):
    result = run_python(["-c", SETTINGS_PROBE], **overrides)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class ProductionSettingsTests(SimpleTestCase):
    def test_render_configuration(self):
        values = production_settings()
        self.assertFalse(values["DEBUG"])
        self.assertIn("clipcash-example.onrender.com", values["ALLOWED_HOSTS"])
        self.assertIn("https://clipcash-example.onrender.com", values["CSRF_TRUSTED_ORIGINS"])
        self.assertEqual(values["SECURE_PROXY_SSL_HEADER"], ["HTTP_X_FORWARDED_PROTO", "https"])
        self.assertTrue(values["SECURE_SSL_REDIRECT"])
        self.assertEqual(values["SECURE_HSTS_SECONDS"], 0)  # Enabled only after HTTPS is verified.
        self.assertTrue(values["SESSION_COOKIE_SECURE"])
        self.assertTrue(values["CSRF_COOKIE_SECURE"])
        self.assertEqual(values["db"], {
            "ENGINE": "django.db.backends.postgresql", "NAME": "clipcash",
            "HOST": "clipcash-db.invalid", "PORT": 5432,
            "OPTIONS": {"sslmode": "require"}, "CONN_MAX_AGE": 60,
        })
        self.assertEqual(values["TEST_NAME"], "test_clipcash_db")
        self.assertEqual(values["STATICFILES_BACKEND"], "whitenoise.storage.CompressedManifestStaticFilesStorage")
        self.assertEqual(values["THROTTLE_BACKEND"], "database")
        self.assertFalse(values["settings_mention_url"])

    def test_financial_features_stay_locked(self):
        values = production_settings()
        self.assertEqual(values["PAYMENTS_MODE"], "disabled")
        self.assertFalse(values["sandbox_active"])
        self.assertEqual(values["financial_enabled"], [])
        self.assertEqual(values["VIDEO_PROVIDER"], "bunny")

    def test_restricted_demo_by_default(self):
        values = production_settings()
        self.assertTrue(values["RESTRICTED_DEMO_MODE"])
        self.assertFalse(values["registration_enabled"])
        self.assertEqual(values["home_view"], "home")

    def test_restricted_demo_can_be_turned_off_explicitly(self):
        self.assertFalse(production_settings(DJANGO_RESTRICTED_DEMO="False")["RESTRICTED_DEMO_MODE"])

    def test_sslmode_in_url_is_kept(self):
        values = production_settings(
            DJANGO_DB_SSLMODE=None,
            DATABASE_URL=PRODUCTION_ENV["DATABASE_URL"] + "?sslmode=verify-full",
        )
        self.assertEqual(values["db"]["OPTIONS"], {"sslmode": "verify-full"})

    def test_non_postgresql_url_is_refused(self):
        result = run_python(["-c", SETTINGS_PROBE], DATABASE_URL="sqlite:///clipcash.sqlite3")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DATABASE_URL must be a PostgreSQL URL", result.stderr)

    def test_render_hostname_is_optional(self):
        values = production_settings(RENDER_EXTERNAL_HOSTNAME=None, DJANGO_ALLOWED_HOSTS="clipcash.example")
        self.assertEqual(values["ALLOWED_HOSTS"], ["clipcash.example"])
        self.assertEqual(values["CSRF_TRUSTED_ORIGINS"], [])


class DeployCheckTests(SimpleTestCase):
    def check_deploy(self, **overrides):
        return run_python(["manage.py", "check", "--deploy"], **overrides)

    def test_production_configuration_has_no_errors(self):
        result = self.check_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("core.W002", result.stderr + result.stdout)

    def test_sandbox_payments_fail_the_deploy_check(self):
        result = self.check_deploy(DJANGO_PAYMENTS_MODE="sandbox")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("payments.E002", result.stderr)

    def test_unencrypted_remote_database_warns(self):
        result = self.check_deploy(DJANGO_DB_SSLMODE=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("core.W002", result.stderr + result.stdout)


@override_settings(ALLOWED_HOSTS=["clipcash.example"], SECURE_SSL_REDIRECT=True)
class HealthCheckTests(TestCase):
    def test_healthy(self):
        # Platform probes may use an internal Host header and plain HTTP.
        response = self.client.get("/healthz", HTTP_HOST="10.0.0.7:10000")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(self.client.head("/healthz").status_code, 200)

    def test_other_requests_keep_host_validation_and_https_redirect(self):
        self.assertEqual(self.client.get("/accounts/login/", HTTP_HOST="10.0.0.7").status_code, 400)
        response = self.client.get("/accounts/login/", HTTP_HOST="clipcash.example")
        self.assertEqual(response.status_code, 301)
        self.assertTrue(response["Location"].startswith("https://clipcash.example/"))

    def test_only_get_and_head(self):
        response = self.client.post("/healthz", HTTP_HOST="clipcash.example", secure=True)
        self.assertNotEqual(response.content, b"ok")

    def test_database_unavailable(self):
        with mock.patch("apps.core.middleware.connection.cursor",
                        side_effect=OperationalError("connection to server at db-host failed")), \
                self.assertLogs("clipcash.health", "ERROR") as logs:
            response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.content, b"unavailable")
        self.assertNotIn("db-host", "\n".join(logs.output))

    def test_maintenance_mode_does_not_fail_the_probe(self):
        cache.clear()
        root = User.objects.create_superuser("root@example.com", PASSWORD)
        version = SiteConfiguration.objects.get().version
        services.update_site_configuration(root, expected_version=version, changes={"maintenance_mode": True})
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        response = self.client.get(reverse("accounts:register"), HTTP_HOST="clipcash.example", secure=True)
        self.assertEqual(response.status_code, 503)


class TokenRedactionTests(SimpleTestCase):
    def test_account_link_tokens_are_removed(self):
        record = logging.LogRecord("django.request", logging.ERROR, __file__, 1, "Internal Server Error: %s",
                                   ("/accounts/activate/MTIz/c4x9-0a1b2c3d4e5f6/",), None)
        self.assertTrue(RedactTokenPaths().filter(record))
        self.assertEqual(record.getMessage(), "Internal Server Error: /accounts/activate/[redacted]/")

    def test_other_messages_are_unchanged(self):
        record = logging.LogRecord("django.request", logging.ERROR, __file__, 1, "Internal Server Error: %s",
                                   ("/dashboard/",), None)
        RedactTokenPaths().filter(record)
        self.assertEqual(record.getMessage(), "Internal Server Error: /dashboard/")
