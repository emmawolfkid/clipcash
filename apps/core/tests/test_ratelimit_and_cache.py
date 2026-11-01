import threading
from datetime import timedelta
from unittest import mock, skipUnless

from django.core.cache import caches
from django.core.management import call_command
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.helpers import PASSWORD, AccountsTestCase, make_user
from apps.accounts.throttling import Throttle
from apps.core import ratelimit, services
from apps.core.models import RateLimitCounter

DATABASE_CACHE = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache",
                              "LOCATION": "clipcash_test_cache", "KEY_PREFIX": "clipcash", "TIMEOUT": 300}}


@override_settings(ACCOUNTS_THROTTLE_BACKEND="database")
class DatabaseRateLimitTests(TestCase):
    def test_limit_reset_and_hashed_keys(self):
        throttle = Throttle("t-db", limit=2, window_seconds=60)
        throttle.hit("victim@example.com")
        self.assertFalse(throttle.is_limited("victim@example.com"))
        throttle.hit("victim@example.com")
        self.assertTrue(throttle.is_limited("victim@example.com"))
        self.assertFalse(RateLimitCounter.objects.filter(key__contains="victim").exists())
        throttle.reset("victim@example.com")
        self.assertFalse(throttle.is_limited("victim@example.com"))

    def test_window_expiry_restarts_count(self):
        throttle = Throttle("t-db", limit=2, window_seconds=60)
        throttle.hit("a")
        throttle.hit("a")
        RateLimitCounter.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertFalse(throttle.is_limited("a"))
        self.assertEqual(ratelimit.hit(throttle._key("a"), 60), 1)

    def test_store_outage_fails_closed(self):
        throttle = Throttle("t-db", limit=100, window_seconds=60)
        with mock.patch("apps.core.ratelimit.count", side_effect=RuntimeError("db down")):
            with self.assertLogs("clipcash.security", "ERROR"):
                self.assertTrue(throttle.is_limited("a"))

    def test_prune_job_removes_expired_counters(self):
        from apps.accounts.tests.helpers import run_worker
        from apps.core.jobs.services import enqueue

        ratelimit.hit("throttle:x:1", 60)
        RateLimitCounter.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        enqueue("core.prune_rate_limits", {})
        run_worker()
        self.assertFalse(RateLimitCounter.objects.exists())


@override_settings(ACCOUNTS_THROTTLE_BACKEND="database")
class LoginThrottleWithDatabaseBackendTests(AccountsTestCase):
    def test_login_lockout_uses_shared_counters(self):
        make_user()
        url = reverse("accounts:login")
        for _ in range(5):
            self.client.post(url, {"username": "member@example.com", "password": "wrong"})
        response = self.client.post(url, {"username": "member@example.com", "password": PASSWORD})
        self.assertEqual([e.code for e in response.context["form"].non_field_errors().as_data()], ["throttled"])
        self.assertTrue(RateLimitCounter.objects.exists())


class SharedDatabaseCacheTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with override_settings(CACHES=DATABASE_CACHE):
            call_command("createcachetable", verbosity=0)

    def test_site_configuration_cache_and_invalidation(self):
        from django.contrib.auth import get_user_model

        with override_settings(CACHES=DATABASE_CACHE):
            caches["default"].clear()
            root = get_user_model().objects.create_superuser("root@example.com", PASSWORD)
            self.assertTrue(services.get_site_configuration().registration_enabled)
            services.update_site_configuration(root, expected_version=1, changes={"registration_enabled": False})
            self.assertFalse(services.get_site_configuration().registration_enabled)

    def test_keys_are_namespaced_with_explicit_ttl(self):
        with override_settings(CACHES=DATABASE_CACHE):
            cache = caches["default"]
            cache.set("probe", "value")
            self.assertEqual(cache.make_key("probe"), "clipcash:1:probe")
            self.assertEqual(cache.default_timeout, 300)

    def test_health_probe_detects_outage(self):
        from apps.core.jobs.builtin import probe_cache

        self.assertTrue(probe_cache())
        with mock.patch("apps.core.jobs.builtin.cache.set", side_effect=ConnectionError):
            self.assertFalse(probe_cache())


@skipUnless(connection.vendor == "postgresql", "Atomicity is verified on PostgreSQL.")
class ConcurrentCounterTests(TransactionTestCase):
    def test_concurrent_hits_are_never_lost(self):
        key = "throttle:concurrency:test"
        barrier = threading.Barrier(10)

        def worker():
            try:
                barrier.wait()
                for _ in range(5):
                    ratelimit.hit(key, 60)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(ratelimit.count(key), 50)
