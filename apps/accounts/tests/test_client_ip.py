"""Phase 14-B (B1, audit CC-004): client IP resolution behind reverse proxies and login throttling."""

from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import reverse

from apps.accounts.checks import _check_client_ip_configuration
from apps.accounts.throttling import client_ip, client_ip_for_limits, resolve_client_ip
from apps.audit.models import AuditEvent

from .helpers import PASSWORD, AccountsTestCase, make_user

LOGIN_URL = reverse("accounts:login")
PROXY = "10.21.157.68"      # Render load balancer (REMOTE_ADDR seen by Django).
EDGE = "104.22.17.40"       # Cloudflare edge, appended by the load balancer.
RENDER = {"ACCOUNTS_TRUSTED_PROXY_COUNT": 2}


def request(remote=PROXY, forwarded=None):
    meta = {"REMOTE_ADDR": remote}
    if forwarded is not None:
        meta["HTTP_X_FORWARDED_FOR"] = forwarded
    return RequestFactory().get("/", **meta)


class ResolutionTests(SimpleTestCase):
    @override_settings(ACCOUNTS_TRUSTED_PROXY_COUNT=0)
    def test_direct_request_uses_the_connection_address(self):
        self.assertEqual(resolve_client_ip(request(remote="203.0.113.5", forwarded="1.2.3.4")),
                         ("203.0.113.5", True))

    @override_settings(**RENDER)
    def test_trusted_proxies_give_the_client_address(self):
        self.assertEqual(resolve_client_ip(request(forwarded=f"198.51.100.7, {EDGE}")), ("198.51.100.7", True))

    @override_settings(**RENDER)
    def test_spoofed_forwarded_entries_are_ignored(self):
        for spoof in ("6.6.6.6", "6.6.6.6, 7.7.7.7", "not-an-ip"):
            with self.subTest(spoof=spoof):
                self.assertEqual(resolve_client_ip(request(forwarded=f"{spoof}, 198.51.100.7, {EDGE}")),
                                 ("198.51.100.7", True))

    @override_settings(**RENDER)
    def test_misconfiguration_is_unresolved_logged_and_excluded_from_limits(self):
        for forwarded in (None, "198.51.100.7", f"garbage, {EDGE}"):
            with self.subTest(forwarded=forwarded), self.assertLogs("clipcash.security", "WARNING") as logs:
                req = request(forwarded=forwarded)
                self.assertEqual(resolve_client_ip(req), (PROXY, False))
                self.assertIsNone(client_ip_for_limits(req))
                self.assertEqual(client_ip(req), PROXY)  # Still recorded for audit, as the proxy.
            self.assertIn("client_ip_unresolved", logs.output[0])

    @override_settings(**RENDER)
    def test_result_is_computed_once_per_request(self):
        req = request(forwarded="198.51.100.7")
        with self.assertLogs("clipcash.security", "WARNING") as logs:
            resolve_client_ip(req)
            resolve_client_ip(req)
        self.assertEqual(len(logs.output), 1)


class DeployCheckTests(SimpleTestCase):
    def ids(self):
        return [message.id for message in _check_client_ip_configuration()]

    @override_settings(SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"), DEPLOYED_ON_RENDER=False,
                       ACCOUNTS_TRUSTED_PROXY_COUNT=0)
    def test_proxy_without_trusted_count_is_an_error(self):
        self.assertEqual(self.ids(), ["accounts.E004"])

    @override_settings(SECURE_PROXY_SSL_HEADER=None, DEPLOYED_ON_RENDER=True, ACCOUNTS_TRUSTED_PROXY_COUNT=0)
    def test_render_without_trusted_count_is_an_error(self):
        self.assertEqual(self.ids(), ["accounts.E004"])

    @override_settings(SECURE_PROXY_SSL_HEADER=None, DEPLOYED_ON_RENDER=True, ACCOUNTS_TRUSTED_PROXY_COUNT=1)
    def test_render_with_one_proxy_warns(self):
        self.assertEqual(self.ids(), ["accounts.W002"])

    @override_settings(SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"), DEPLOYED_ON_RENDER=True,
                       ACCOUNTS_TRUSTED_PROXY_COUNT=2)
    def test_render_with_two_proxies_passes(self):
        self.assertEqual(self.ids(), [])

    @override_settings(SECURE_PROXY_SSL_HEADER=None, DEPLOYED_ON_RENDER=False, ACCOUNTS_TRUSTED_PROXY_COUNT=0)
    def test_direct_deployment_needs_no_count(self):
        self.assertEqual(self.ids(), [])


class LoginBehindProxyTests(AccountsTestCase):
    def setUp(self):
        super().setUp()
        self.member = make_user("member@example.com")
        self.other = make_user("other@example.com")

    def login(self, email, password, client_address=None, forwarded=None):
        extra = {"REMOTE_ADDR": PROXY}
        if forwarded is not None:
            extra["HTTP_X_FORWARDED_FOR"] = forwarded
        elif client_address:
            extra["HTTP_X_FORWARDED_FOR"] = f"{client_address}, {EDGE}"
        return self.client.post(LOGIN_URL, {"username": email, "password": password}, **extra)

    def assert_signed_in(self, response):
        self.assertEqual(response.status_code, 302, response.content[:300])
        self.client.logout()

    @override_settings(**RENDER)
    def test_users_behind_one_proxy_are_limited_separately(self):
        attacker = "198.51.100.66"
        for i in range(50):  # Per-IP ceiling: 50 failures in 15 minutes.
            self.login(f"nobody{i}@example.com", "wrong-password", attacker)
        self.assertContains(self.login("other@example.com", PASSWORD, attacker), "Too many failed sign-in attempts")
        self.assert_signed_in(self.login("member@example.com", PASSWORD, "203.0.113.20"))

    @override_settings(**RENDER)
    def test_repeated_failures_still_lock_the_account_from_that_client(self):
        for _ in range(5):
            self.login("member@example.com", "wrong-password", "198.51.100.66")
        self.assertContains(self.login("member@example.com", PASSWORD, "198.51.100.66"),
                            "Too many failed sign-in attempts")
        self.assert_signed_in(self.login("member@example.com", PASSWORD, "203.0.113.20"))  # Victim elsewhere.

    @override_settings(**RENDER)
    def test_spoofed_header_does_not_escape_the_limit(self):
        for i in range(5):
            self.login("member@example.com", "wrong-password", forwarded=f"6.6.6.{i}, 198.51.100.66, {EDGE}")
        self.assertContains(self.login("member@example.com", PASSWORD, forwarded=f"9.9.9.9, 198.51.100.66, {EDGE}"),
                            "Too many failed sign-in attempts")

    @override_settings(**RENDER)
    def test_audit_records_the_client_not_the_proxy(self):
        self.login("member@example.com", "wrong-password", "198.51.100.66")
        self.assertEqual(AuditEvent.objects.filter(action="auth.login.failed").get().ip_address, "198.51.100.66")

    @override_settings(**RENDER)
    def test_misconfigured_proxy_does_not_make_one_shared_limit(self):
        """Entries missing (e.g. a wrong proxy count): per-IP limits are skipped rather than
        applied to every visitor at once; the per-account ceiling still holds."""
        with self.assertLogs("clipcash.security", "WARNING"):
            for i in range(60):
                self.login(f"nobody{i}@example.com", "wrong-password", forwarded="")
            self.assert_signed_in(self.login("member@example.com", PASSWORD, forwarded=""))
            for _ in range(50):
                self.login("other@example.com", "wrong-password", forwarded="")
            self.assertContains(self.login("other@example.com", PASSWORD, forwarded=""),
                                "Too many failed sign-in attempts")

    @override_settings(ACCOUNTS_TRUSTED_PROXY_COUNT=0)
    def test_direct_connections_keep_per_ip_limits(self):
        for i in range(50):
            self.client.post(LOGIN_URL, {"username": f"nobody{i}@example.com", "password": "wrong"},
                             REMOTE_ADDR="198.51.100.66")
        response = self.client.post(LOGIN_URL, {"username": "member@example.com", "password": PASSWORD},
                                    REMOTE_ADDR="198.51.100.66")
        self.assertContains(response, "Too many failed sign-in attempts")


@override_settings(**RENDER)
class HealthDiagnosticsTests(AccountsTestCase):
    def test_super_admin_sees_how_their_address_was_resolved(self):
        from apps.accounts.tests.helpers import User

        root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.client.force_login(root)
        response = self.client.get(reverse("administration:health"), REMOTE_ADDR=PROXY,
                                   HTTP_X_FORWARDED_FOR=f"6.6.6.6, 198.51.100.7, {EDGE}")
        diagnostics = response.context["client_ip"]
        self.assertEqual((diagnostics["address"], diagnostics["resolved"], diagnostics["trusted_proxies"]),
                         ("198.51.100.7", True, 2))
        self.assertContains(response, "Client address resolution")

    def test_other_staff_do_not(self):
        from apps.accounts.tests.helpers import User

        self.client.force_login(make_user("cm@example.com", role=User.Role.CONTENT_MANAGER))
        response = self.client.get(reverse("administration:health"), REMOTE_ADDR=PROXY)
        self.assertIsNone(response.context["client_ip"])
        self.assertNotContains(response, "Client address resolution")
