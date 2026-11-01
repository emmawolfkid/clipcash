"""
Fixed-window rate limits.

Two storage backends (settings.ACCOUNTS_THROTTLE_BACKEND):

* ``database`` (production default): atomic PostgreSQL counters
  (apps.core.ratelimit). Consistent across every process and server, with no
  extra service.
* ``cache``: the Django cache (development and tests). Production must not
  use a per-process cache: with ACCOUNTS_THROTTLE_REQUIRE_SHARED_CACHE on, a
  per-process cache fails closed and the ``accounts.E001`` check reports it.
  Note that Django's database cache cannot increment atomically, so it can
  undercount under concurrency; prefer the ``database`` backend.

Whenever storage errors, limits fail closed: the request is treated as limited.
"""

import hashlib
import ipaddress
import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import caches

from apps.core import ratelimit

security_log = logging.getLogger("clipcash.security")

PER_PROCESS_CACHE_BACKENDS = {
    "django.core.cache.backends.locmem.LocMemCache",
    "django.core.cache.backends.dummy.DummyCache",
}


def throttle_cache():
    return caches[settings.ACCOUNTS_THROTTLE_CACHE]


def cache_is_shared():
    backend = settings.CACHES[settings.ACCOUNTS_THROTTLE_CACHE]["BACKEND"]
    return backend not in PER_PROCESS_CACHE_BACKENDS


def uses_database():
    return settings.ACCOUNTS_THROTTLE_BACKEND == "database"


def _fail_closed():
    return (not uses_database() and settings.ACCOUNTS_THROTTLE_REQUIRE_SHARED_CACHE
            and not cache_is_shared())


@dataclass(frozen=True)
class Throttle:
    scope: str
    limit: int
    window_seconds: int

    def _key(self, *parts):
        digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()
        return f"throttle:{self.scope}:{digest}"[:100]

    def is_limited(self, *parts):
        if _fail_closed():
            security_log.error("throttle_misconfigured scope=%s (per-process cache)", self.scope)
            return True
        try:
            if uses_database():
                return ratelimit.count(self._key(*parts)) >= self.limit
            return throttle_cache().get(self._key(*parts), 0) >= self.limit
        except Exception:
            security_log.exception("throttle_store_error scope=%s", self.scope)
            return True

    def hit(self, *parts):
        key = self._key(*parts)
        try:
            if uses_database():
                ratelimit.hit(key, self.window_seconds)
                return
            cache = throttle_cache()
            if not cache.add(key, 1, self.window_seconds):
                try:
                    cache.incr(key)
                except ValueError:
                    cache.set(key, 1, self.window_seconds)  # Expired between add and incr.
        except Exception:
            security_log.exception("throttle_store_error scope=%s", self.scope)

    def reset(self, *parts):
        try:
            if uses_database():
                ratelimit.reset(self._key(*parts))
            else:
                throttle_cache().delete(self._key(*parts))
        except Exception:
            security_log.exception("throttle_store_error scope=%s", self.scope)


# Failed logins. The tight limit is per (account, IP) so an attacker cannot lock a
# victim out from elsewhere; the per-account ceiling stops distributed guessing;
# the per-IP limit is generous because mobile carriers share IPs (CGNAT).
LOGIN_FAILURES_PER_ACCOUNT_IP = Throttle("login-account-ip", limit=5, window_seconds=15 * 60)
LOGIN_FAILURES_PER_ACCOUNT = Throttle("login-account", limit=50, window_seconds=60 * 60)
LOGIN_FAILURES_PER_IP = Throttle("login-ip", limit=50, window_seconds=15 * 60)

REGISTRATIONS_PER_IP = Throttle("register-ip", limit=10, window_seconds=60 * 60)
EMAIL_REQUESTS_PER_IP = Throttle("email-request-ip", limit=20, window_seconds=60 * 60)
EMAIL_REQUESTS_PER_ACCOUNT = Throttle("email-request-account", limit=3, window_seconds=60 * 60)


def client_ip(request):
    """
    Client address. X-Forwarded-For is used only when ACCOUNTS_TRUSTED_PROXY_COUNT
    reverse proxies are configured, taking the address the outermost trusted
    proxy saw; any client-supplied prefix is ignored.
    """
    if request is None:
        return ""
    remote_addr = request.META.get("REMOTE_ADDR", "")
    proxies = settings.ACCOUNTS_TRUSTED_PROXY_COUNT
    if proxies <= 0:
        return remote_addr
    forwarded = [part.strip() for part in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")]
    forwarded = [part for part in forwarded if part]
    if len(forwarded) < proxies:
        return remote_addr
    candidate = forwarded[-proxies]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return remote_addr
