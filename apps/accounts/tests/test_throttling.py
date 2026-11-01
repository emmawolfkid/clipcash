from unittest import mock

from django.core.checks import run_checks
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, SimpleTestCase, override_settings

from apps.accounts.throttling import Throttle, client_ip

from .helpers import AccountsTestCase

SHARED_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "unused_cache_table",
    }
}


class ClientIpTests(SimpleTestCase):
    def request(self, remote="10.0.0.1", forwarded=None):
        meta = {"REMOTE_ADDR": remote}
        if forwarded is not None:
            meta["HTTP_X_FORWARDED_FOR"] = forwarded
        return RequestFactory().get("/", **meta)

    @override_settings(ACCOUNTS_TRUSTED_PROXY_COUNT=0)
    def test_forwarded_header_ignored_without_trusted_proxy(self):
        self.assertEqual(client_ip(self.request(forwarded="1.2.3.4")), "10.0.0.1")

    @override_settings(ACCOUNTS_TRUSTED_PROXY_COUNT=1)
    def test_uses_address_seen_by_trusted_proxy(self):
        # Client spoofs a prefix; only the entry appended by our proxy counts.
        self.assertEqual(client_ip(self.request(forwarded="6.6.6.6, 198.51.100.7")), "198.51.100.7")

    @override_settings(ACCOUNTS_TRUSTED_PROXY_COUNT=2)
    def test_two_trusted_proxies(self):
        self.assertEqual(
            client_ip(self.request(forwarded="6.6.6.6, 198.51.100.7, 10.1.1.1")), "198.51.100.7"
        )

    @override_settings(ACCOUNTS_TRUSTED_PROXY_COUNT=1)
    def test_invalid_or_missing_header_falls_back(self):
        self.assertEqual(client_ip(self.request(forwarded="not-an-ip")), "10.0.0.1")
        self.assertEqual(client_ip(self.request()), "10.0.0.1")


class ThrottleTests(AccountsTestCase):
    def test_limit_and_reset(self):
        throttle = Throttle("test", limit=2, window_seconds=60)
        self.assertFalse(throttle.is_limited("a"))
        throttle.hit("a")
        throttle.hit("a")
        self.assertTrue(throttle.is_limited("a"))
        self.assertFalse(throttle.is_limited("b"))
        throttle.reset("a")
        self.assertFalse(throttle.is_limited("a"))

    def test_keys_do_not_expose_identifiers(self):
        throttle = Throttle("test", limit=1, window_seconds=60)
        self.assertNotIn("victim@example.com", throttle._key("victim@example.com"))

    @override_settings(ACCOUNTS_THROTTLE_REQUIRE_SHARED_CACHE=True)
    def test_fails_closed_on_per_process_cache_when_shared_required(self):
        throttle = Throttle("test", limit=100, window_seconds=60)
        with self.assertLogs("clipcash.security", "ERROR"):
            self.assertTrue(throttle.is_limited("a"))

    def test_fails_closed_on_cache_error(self):
        throttle = Throttle("test", limit=100, window_seconds=60)
        broken = mock.Mock()
        broken.get.side_effect = ConnectionError("cache down")
        with mock.patch("apps.accounts.throttling.throttle_cache", return_value=broken):
            with self.assertLogs("clipcash.security", "ERROR"):
                self.assertTrue(throttle.is_limited("a"))
                throttle.hit("a")  # Must not raise.


class SystemCheckTests(SimpleTestCase):
    def check_ids(self):
        return {
            message.id
            for message in run_checks(tags=["security"], include_deployment_checks=True)
        }

    @override_settings(ACCOUNTS_THROTTLE_REQUIRE_SHARED_CACHE=True)
    def test_per_process_cache_rejected_when_shared_required(self):
        self.assertIn("accounts.E001", self.check_ids())

    @override_settings(ACCOUNTS_THROTTLE_REQUIRE_SHARED_CACHE=True, CACHES=SHARED_CACHE)
    def test_shared_cache_accepted(self):
        self.assertNotIn("accounts.E001", self.check_ids())

    @override_settings(DEBUG=False, SESSION_COOKIE_SECURE=False)
    def test_insecure_cookies_rejected_in_production(self):
        self.assertIn("accounts.E002", self.check_ids())

    @override_settings(
        DEBUG=False,
        SESSION_COOKIE_SECURE=True,
        CSRF_COOKIE_SECURE=True,
        AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.ModelBackend"],
    )
    def test_default_backend_rejected_in_production(self):
        self.assertIn("accounts.E003", self.check_ids())

    @override_settings(DEBUG=False, SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True)
    def test_secure_production_settings_accepted(self):
        self.assertFalse({"accounts.E002", "accounts.E003"} & self.check_ids())


class TestRunnerGuardTests(SimpleTestCase):
    def runner(self):
        from config.test_runner import PostgreSQLTestRunner

        return PostgreSQLTestRunner(verbosity=0)

    @override_settings(DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}})
    def test_refuses_non_postgresql(self):
        with self.assertRaises(ImproperlyConfigured):
            self.runner().setup_databases()

    @override_settings(DATABASES={"default": {
        "ENGINE": "django.db.backends.postgresql", "NAME": "clipcash_db", "TEST": {"NAME": "clipcash_db"},
    }})
    def test_refuses_test_name_equal_to_application_database(self):
        with self.assertRaises(ImproperlyConfigured):
            self.runner().setup_databases()

    @override_settings(DATABASES={"default": {
        "ENGINE": "django.db.backends.postgresql", "NAME": "clipcash_db", "TEST": {"NAME": "scratch"},
    }})
    def test_refuses_name_without_test_prefix(self):
        with self.assertRaises(ImproperlyConfigured):
            self.runner().setup_databases()
