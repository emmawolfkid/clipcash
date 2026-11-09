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

# Signed-in account changes. Wrong current passwords: a stolen session must not be able to
# guess the password. Phone attempts: a duplicate number is refused, so attempts are limited.
CURRENT_PASSWORD_FAILURES = Throttle("current-password", limit=5, window_seconds=15 * 60)
PHONE_CHANGE_ATTEMPTS = Throttle("phone-change", limit=10, window_seconds=60 * 60)
# The per-address limit has its own scope: otherwise a member could use up someone
# else's password-reset allowance by naming their address here.
EMAIL_CHANGE_REQUESTS_PER_ACCOUNT = Throttle("email-change-account", limit=5, window_seconds=60 * 60)
EMAIL_CHANGE_REQUESTS_PER_ADDRESS = Throttle("email-change-address", limit=3, window_seconds=60 * 60)


# Single-address headers a trusted proxy may set itself (DJANGO_CLIENT_IP_HEADER).
# Anything else (X-Real-IP included) is never read unless it is configured here.
CLIENT_IP_HEADERS = {"CF-Connecting-IP": "HTTP_CF_CONNECTING_IP", "X-Real-IP": "HTTP_X_REAL_IP",
                     "True-Client-IP": "HTTP_TRUE_CLIENT_IP"}


def trusted_proxy_networks():
    """Parsed DJANGO_TRUSTED_PROXY_NETWORKS; invalid entries are skipped here and
    reported by the accounts.E006 deploy check."""
    networks = []
    for value in settings.ACCOUNTS_TRUSTED_PROXY_NETWORKS:
        try:
            networks.append(ipaddress.ip_network(value.strip(), strict=False))
        except ValueError:
            continue
    return networks


def _parse_address(value):
    """One bare IP address, or None (lists, ports, brackets and names are refused)."""
    try:
        return str(ipaddress.ip_address(value.strip()))
    except (ValueError, AttributeError):
        return None


def peer_is_trusted(remote_addr):
    networks = trusted_proxy_networks()
    if not networks:
        return True  # No allow-list: every peer is assumed to be the proxy (Render).
    address = _parse_address(remote_addr)
    if address is None:
        return False
    address = ipaddress.ip_address(address)
    return any(address.version == network.version and address in network for network in networks)


def _resolve(request):
    remote_addr = request.META.get("REMOTE_ADDR", "")
    proxies = settings.ACCOUNTS_TRUSTED_PROXY_COUNT
    header = settings.ACCOUNTS_CLIENT_IP_HEADER
    if proxies <= 0 and not header:
        return remote_addr, True  # Direct connections: forwarded headers are client-supplied.
    if not peer_is_trusted(remote_addr):
        # The peer is not one of our proxies (e.g. a request sent straight to the origin):
        # every forwarded header came from the client, and the peer itself is the client.
        security_log.info("client_ip_untrusted_peer")
        return remote_addr, True
    if header:
        value = request.META.get(CLIENT_IP_HEADERS.get(header, ""), "")
        address = _parse_address(value) if "," not in value else None
        if address is None:
            security_log.warning("client_ip_unresolved header=%s present=%s", header, bool(value))
            return remote_addr, False
        return address, True
    forwarded = [part.strip() for part in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")]
    forwarded = [part for part in forwarded if part]
    if len(forwarded) >= proxies:
        address = _parse_address(forwarded[-proxies])
        if address is not None:
            return address, True
    security_log.warning("client_ip_unresolved forwarded_entries=%s trusted_proxies=%s", len(forwarded), proxies)
    return remote_addr, False


def resolve_client_ip(request):
    """
    (address, resolved).

    Forwarded information is used only when proxies are configured
    (ACCOUNTS_TRUSTED_PROXY_COUNT or ACCOUNTS_CLIENT_IP_HEADER) and, if
    ACCOUNTS_TRUSTED_PROXY_NETWORKS is set, only when the connecting peer
    (REMOTE_ADDR) lies inside one of those networks. A peer outside them reached
    the application directly, so its own address is the client and every forwarded
    header it sent is ignored.

    From a trusted peer: with ACCOUNTS_CLIENT_IP_HEADER (e.g. CF-Connecting-IP), that
    single-address header is the client and X-Forwarded-For is ignored; otherwise the
    entry ``proxies`` from the right of X-Forwarded-For is the address the outermost
    trusted proxy saw, and anything a client put further left is ignored (Render, for
    example, keeps client-supplied entries and appends to them).

    ``resolved`` is False when a trusted proxy's information is missing or malformed
    (fewer forwarded entries than trusted proxies, an invalid entry, a missing header):
    the address returned is then REMOTE_ADDR, the proxy's own and shared by every
    visitor, so callers must not use it to limit or block individual clients.
    """
    if request is None:
        return "", False
    cached = getattr(request, "_clipcash_client_ip", None)
    if cached is not None:
        return cached
    result = _resolve(request)
    try:
        request._clipcash_client_ip = result
    except AttributeError:
        pass
    return result


def client_ip(request):
    """Best-known client address (for audit records); see resolve_client_ip."""
    return resolve_client_ip(request)[0]


def client_ip_for_limits(request):
    """The client address when it identifies one client, else None (skip per-IP limits)."""
    address, resolved = resolve_client_ip(request)
    return address if resolved and address else None
