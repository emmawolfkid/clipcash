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


# RG-03: a trusted-proxy boundary (e.g. NetPoa behind Cloudflare). The ranges below are
# real Cloudflare ranges; the addresses inside them stand in for Cloudflare edges.
CF_NETWORKS = ["104.16.0.0/13", "173.245.48.0/20", "2400:cb00::/32"]
CF_EDGE = EDGE                      # 104.22.17.40, inside 104.16.0.0/13.
CF_EDGE_V6 = "2400:cb00:2049:1::a29f:1a3c"
ATTACKER = "203.0.113.66"           # Reaches the origin directly, outside every trusted network.
NETPOA = {"ACCOUNTS_TRUSTED_PROXY_COUNT": 1, "ACCOUNTS_TRUSTED_PROXY_NETWORKS": CF_NETWORKS}
NETPOA_HEADER = {"ACCOUNTS_TRUSTED_PROXY_COUNT": 0, "ACCOUNTS_TRUSTED_PROXY_NETWORKS": CF_NETWORKS,
                 "ACCOUNTS_CLIENT_IP_HEADER": "CF-Connecting-IP"}
HEADER_NAMES = {"forwarded": "HTTP_X_FORWARDED_FOR", "cf": "HTTP_CF_CONNECTING_IP", "real": "HTTP_X_REAL_IP",
                "true_client": "HTTP_TRUE_CLIENT_IP"}


def boundary_request(remote, **headers):
    meta = {"REMOTE_ADDR": remote}
    meta.update({HEADER_NAMES[key]: value for key, value in headers.items()})
    return RequestFactory().get("/", **meta)


class TrustedBoundaryResolutionTests(SimpleTestCase):
    @override_settings(**NETPOA)
    def test_requests_through_the_proxy_give_the_client_address(self):
        self.assertEqual(resolve_client_ip(boundary_request(CF_EDGE, forwarded="198.51.100.7")),
                         ("198.51.100.7", True))
        self.assertEqual(resolve_client_ip(boundary_request(CF_EDGE_V6, forwarded="2001:db8::7")),
                         ("2001:db8::7", True))

    @override_settings(**NETPOA)
    def test_client_entries_left_of_the_proxy_entry_are_ignored(self):
        self.assertEqual(resolve_client_ip(boundary_request(CF_EDGE, forwarded="6.6.6.6, 198.51.100.7")),
                         ("198.51.100.7", True))

    @override_settings(**NETPOA)
    def test_direct_origin_requests_ignore_every_forwarded_header(self):
        for headers in ({}, {"forwarded": "6.6.6.6"}, {"forwarded": "6.6.6.6, 7.7.7.7"},
                        {"cf": "6.6.6.6"}, {"real": "6.6.6.6"}, {"true_client": "6.6.6.6"},
                        {"forwarded": "not-an-ip"}, {"forwarded": f"6.6.6.6, {CF_EDGE}"}):
            with self.subTest(headers=headers):
                req = boundary_request(ATTACKER, **headers)
                self.assertEqual(resolve_client_ip(req), (ATTACKER, True))
                self.assertEqual(client_ip_for_limits(req), ATTACKER)  # Per-IP limits still apply.

    @override_settings(**NETPOA)
    def test_unusual_peer_addresses_are_not_trusted(self):
        for remote in ("", "not-an-ip", "104.24.0.1", "104.22.17.40x"):
            with self.subTest(remote=remote):
                self.assertEqual(resolve_client_ip(boundary_request(remote, forwarded="6.6.6.6")), (remote, True))

    @override_settings(**NETPOA)
    def test_missing_or_malformed_proxy_information_is_unresolved(self):
        for forwarded in (None, "", "garbage", "198.51.100.7:443", "[2001:db8::7]"):
            with self.subTest(forwarded=forwarded), self.assertLogs("clipcash.security", "WARNING"):
                headers = {} if forwarded is None else {"forwarded": forwarded}
                req = boundary_request(CF_EDGE, **headers)
                self.assertEqual(resolve_client_ip(req), (CF_EDGE, False))
                self.assertIsNone(client_ip_for_limits(req))

    @override_settings(**NETPOA_HEADER)
    def test_configured_header_wins_over_conflicting_headers(self):
        req = boundary_request(CF_EDGE, cf="198.51.100.7", forwarded="6.6.6.6", real="7.7.7.7")
        self.assertEqual(resolve_client_ip(req), ("198.51.100.7", True))

    @override_settings(**NETPOA_HEADER)
    def test_configured_header_from_an_untrusted_peer_is_ignored(self):
        self.assertEqual(resolve_client_ip(boundary_request(ATTACKER, cf="198.51.100.7")), (ATTACKER, True))

    @override_settings(**NETPOA_HEADER)
    def test_malformed_configured_header_is_unresolved(self):
        for value in ("", "198.51.100.7, 6.6.6.6", "198.51.100.7:80", "example.com"):
            with self.subTest(value=value), self.assertLogs("clipcash.security", "WARNING"):
                self.assertEqual(resolve_client_ip(boundary_request(CF_EDGE, cf=value)), (CF_EDGE, False))

    @override_settings(ACCOUNTS_TRUSTED_PROXY_COUNT=0)
    def test_x_real_ip_and_cloudflare_headers_are_never_read_by_default(self):
        req = boundary_request("198.51.100.9", cf="6.6.6.6", real="7.7.7.7", true_client="8.8.8.8")
        self.assertEqual(resolve_client_ip(req), ("198.51.100.9", True))

    @override_settings(**RENDER)
    def test_render_configuration_is_unchanged(self):
        """No network list on Render: its internal load balancer is the only peer."""
        self.assertEqual(resolve_client_ip(request(forwarded=f"6.6.6.6, 198.51.100.7, {EDGE}")),
                         ("198.51.100.7", True))


class TrustedBoundaryDeployCheckTests(SimpleTestCase):
    BASE = {"SECURE_PROXY_SSL_HEADER": None, "DEPLOYED_ON_RENDER": False, "ACCOUNTS_PROXY_ORIGIN_RESTRICTED": False,
            "ACCOUNTS_CLIENT_IP_HEADER": "", "ACCOUNTS_TRUSTED_PROXY_NETWORKS": []}

    def ids(self, **values):
        with self.settings(**{**self.BASE, **values}):
            return sorted(message.id for message in _check_client_ip_configuration())

    def test_trusting_any_peer_outside_render_fails_closed(self):
        self.assertEqual(self.ids(ACCOUNTS_TRUSTED_PROXY_COUNT=1), ["accounts.E007"])

    def test_a_network_list_or_an_attested_origin_lock_passes(self):
        self.assertEqual(self.ids(**NETPOA), [])
        self.assertEqual(self.ids(ACCOUNTS_TRUSTED_PROXY_COUNT=1, ACCOUNTS_PROXY_ORIGIN_RESTRICTED=True), [])

    def test_render_keeps_its_existing_rules(self):
        self.assertEqual(self.ids(DEPLOYED_ON_RENDER=True, ACCOUNTS_TRUSTED_PROXY_COUNT=2), [])

    def test_header_without_networks_is_an_error_even_with_an_attestation(self):
        self.assertEqual(self.ids(ACCOUNTS_TRUSTED_PROXY_COUNT=0, ACCOUNTS_PROXY_ORIGIN_RESTRICTED=True,
                                  ACCOUNTS_CLIENT_IP_HEADER="CF-Connecting-IP"), ["accounts.E005"])

    def test_unsupported_header_and_invalid_networks(self):
        self.assertEqual(self.ids(**{**NETPOA, "ACCOUNTS_CLIENT_IP_HEADER": "X-Client-IP",
                                     "ACCOUNTS_TRUSTED_PROXY_NETWORKS": ["104.16.0.0/13", "cloudflare"]}),
                         ["accounts.E006", "accounts.E008"])

    def test_networks_without_any_proxy_setting_warn(self):
        self.assertEqual(self.ids(ACCOUNTS_TRUSTED_PROXY_COUNT=0, ACCOUNTS_TRUSTED_PROXY_NETWORKS=CF_NETWORKS),
                         ["accounts.W003"])


@override_settings(**NETPOA)
class SpoofedHeaderLoginTests(AccountsTestCase):
    """Spoofed headers sent straight to the origin cannot reset or escape sign-in limits."""

    def setUp(self):
        super().setUp()
        self.member = make_user("member@example.com")

    def post(self, email, password, remote, **headers):
        extra = {"REMOTE_ADDR": remote}
        extra.update({HEADER_NAMES[key]: value for key, value in headers.items()})
        return self.client.post(LOGIN_URL, {"username": email, "password": password}, **extra)

    def test_rotating_spoofed_headers_does_not_escape_the_account_limit(self):
        for i in range(5):
            self.post("member@example.com", "wrong-password", ATTACKER,
                      forwarded=f"6.6.6.{i}", cf=f"7.7.7.{i}", real=f"8.8.8.{i}")
        response = self.post("member@example.com", PASSWORD, ATTACKER, forwarded="9.9.9.9", cf="9.9.9.9")
        self.assertContains(response, "Too many failed sign-in attempts")
        # The real member, arriving through the proxy, is not locked out by the attacker.
        response = self.post("member@example.com", PASSWORD, CF_EDGE, forwarded="198.51.100.20")
        self.assertEqual(response.status_code, 302)

    def test_rotating_spoofed_headers_does_not_escape_the_per_ip_limit(self):
        for i in range(50):
            self.post(f"nobody{i}@example.com", "wrong-password", ATTACKER, forwarded=f"6.6.{i}.1")
        response = self.post("member@example.com", PASSWORD, ATTACKER, forwarded="198.51.100.20")
        self.assertContains(response, "Too many failed sign-in attempts")

    def test_spoofed_headers_cannot_frame_another_address_in_the_audit_log(self):
        self.post("member@example.com", "wrong-password", ATTACKER, forwarded="198.51.100.20")
        self.assertEqual(AuditEvent.objects.filter(action="auth.login.failed").get().ip_address, ATTACKER)
