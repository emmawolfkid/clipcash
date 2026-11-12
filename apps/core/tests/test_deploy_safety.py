"""
RG-03: unsafe deployment configurations fail `manage.py check --deploy --fail-level ERROR`
(the command the Render build and the NetPoa runbook run). The end-to-end cases run the
real command in a subprocess with a fake, nonsecret environment; nothing connects to a
database or a remote host.
"""

from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.core import checks
from apps.core.tests.test_deployment import run_python
from apps.videos.checks import check_bunny_configuration

CF_NETWORKS = "173.245.48.0/20,103.21.244.0/22,104.16.0.0/13,2400:cb00::/32"
# A NetPoa-style deployment: Passenger receives HTTPS itself, Cloudflare is the only trusted peer.
NETPOA_ENV = {
    "RENDER_EXTERNAL_HOSTNAME": None,
    "DJANGO_ALLOWED_HOSTS": "demo.clipcash.example",
    "DJANGO_CSRF_TRUSTED_ORIGINS": "https://demo.clipcash.example",
    "DATABASE_URL": "postgres://ccapp:example-password@localhost:5432/clipcash",
    "DJANGO_DB_SSLMODE": None,
    "DJANGO_SECURE_PROXY_SSL_HEADER": "False",
    "DJANGO_TRUSTED_PROXY_COUNT": "1",
    "DJANGO_TRUSTED_PROXY_NETWORKS": CF_NETWORKS,
}


def ids(messages):
    return sorted(message.id for message in messages)


class DeployCheckUnitTests(SimpleTestCase):
    @override_settings(DEBUG=True)
    def test_debug_on_is_an_error(self):
        self.assertEqual(ids(checks.check_debug_off(None)), ["core.E002"])

    def test_weak_or_placeholder_secrets_are_errors(self):
        for secret in ("", "short", "a" * 60, "django-insecure-" + "x1y2z3" * 10,
                       "replace-with-a-generated-secret-key"):
            with self.subTest(secret=secret[:20]), override_settings(SECRET_KEY=secret or "x"):
                self.assertEqual(ids(checks.check_secret_key(None)), ["core.E003"])
        with override_settings(SECRET_KEY="Zq8-" + "k3Jx9_pL2mN7vB4cR6tY1wE5uI0oA8sD" * 2):
            self.assertEqual(checks.check_secret_key(None), [])

    @override_settings(DEBUG=False)
    def test_open_hosts_and_unsafe_origins_are_errors(self):
        for hosts in ([], ["*"], ["demo.example", "*"], ["."]):
            with self.subTest(hosts=hosts), override_settings(ALLOWED_HOSTS=hosts, CSRF_TRUSTED_ORIGINS=[]):
                self.assertEqual(ids(checks.check_hosts_and_origins(None)), ["core.E004"])
        for origins in (["http://demo.example"], ["https://*"], ["demo.example"]):
            with self.subTest(origins=origins), override_settings(ALLOWED_HOSTS=["demo.example"],
                                                                  CSRF_TRUSTED_ORIGINS=origins):
                self.assertEqual(ids(checks.check_hosts_and_origins(None)), ["core.E005"])
        with override_settings(ALLOWED_HOSTS=["demo.example"],
                               CSRF_TRUSTED_ORIGINS=["https://demo.example", "https://*.example.org"]):
            self.assertEqual(checks.check_hosts_and_origins(None), [])

    @override_settings(DEBUG=False, SECURE_SSL_REDIRECT=False)
    def test_missing_https_redirect_is_an_error(self):
        self.assertEqual(ids(checks.check_https_redirect(None)), ["core.E006"])

    @override_settings(DEBUG=False, RESTRICTED_DEMO_MODE=False, PUBLIC_REGISTRATION_APPROVED=False)
    def test_leaving_restricted_demo_without_approval_keeps_registration_closed(self):
        # NET-02: a warning, not an error. The runtime gate keeps registration closed.
        from apps.core import features

        self.assertEqual(ids(checks.check_restricted_demo(None)), ["core.W003"])
        self.assertFalse(features.is_enabled("registration"))
        with override_settings(PUBLIC_REGISTRATION_APPROVED=True):
            self.assertEqual(checks.check_restricted_demo(None), [])
        with override_settings(RESTRICTED_DEMO_MODE=True):
            self.assertEqual(checks.check_restricted_demo(None), [])

    @override_settings(DEBUG=False, ALLOWED_HOSTS=["clipcash.example", ".clipcash.org"])
    def test_public_base_url_for_emailed_links(self):
        """REG-01: activation and reset links use the configured HTTPS origin in public production."""
        with override_settings(RESTRICTED_DEMO_MODE=False, PUBLIC_BASE_URL=""):
            self.assertEqual(ids(checks.check_public_base_url(None)), ["core.E009"])
        with override_settings(RESTRICTED_DEMO_MODE=True, PUBLIC_BASE_URL=""):
            self.assertEqual(checks.check_public_base_url(None), [])  # The demo keeps request-based links.
        for bad in ("http://clipcash.example", "https://other.example", "https://clipcash.example/path",
                    "https://user@clipcash.example", "clipcash.example", "https://clipcash.example?x=1"):
            for demo in (False, True):
                with self.subTest(value=bad, demo=demo), override_settings(RESTRICTED_DEMO_MODE=demo,
                                                                          PUBLIC_BASE_URL=bad):
                    self.assertEqual(ids(checks.check_public_base_url(None)), ["core.E009"])
        for good in ("https://clipcash.example", "https://www.clipcash.org", "https://clipcash.example:8443"):
            with self.subTest(value=good), override_settings(RESTRICTED_DEMO_MODE=False, PUBLIC_BASE_URL=good):
                self.assertEqual(checks.check_public_base_url(None), [])
        with override_settings(DEBUG=True, RESTRICTED_DEMO_MODE=False, PUBLIC_BASE_URL=""):
            self.assertEqual(checks.check_public_base_url(None), [])

    def test_financial_features_stay_locked(self):
        self.assertEqual(checks.check_financial_locks(None), [])
        from apps.core import features

        locked = next(iter(features.FINANCIAL_FEATURES))
        with mock.patch.object(features, "is_enabled", lambda key, config=None: key == locked):
            self.assertEqual(ids(checks.check_financial_locks(None)), ["core.E008"])

    @override_settings(VIDEO_PROVIDER="bunny", BUNNY_STREAM_LIBRARY_ID="lib-12", BUNNY_STREAM_API_KEY="k",
                       BUNNY_STREAM_CDN_HOSTNAME="https://vz-test.b-cdn.net/videos", BUNNY_STREAM_TOKEN_SECURITY_KEY="t")
    def test_malformed_bunny_values_are_errors(self):
        self.assertEqual(ids(check_bunny_configuration(None)), ["videos.E003", "videos.E004"])
        with override_settings(BUNNY_STREAM_LIBRARY_ID="12345", BUNNY_STREAM_CDN_HOSTNAME="vz-test.b-cdn.net"):
            self.assertEqual(check_bunny_configuration(None), [])


class DeployCommandTests(SimpleTestCase):
    """The real command, as the build runs it."""

    def run_check(self, **overrides):
        return run_python(["manage.py", "check", "--deploy", "--fail-level", "ERROR"], **overrides)

    def assert_fails_with(self, check_id, **overrides):
        result = self.run_check(**overrides)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(check_id, result.stderr + result.stdout)

    def test_render_profile_passes(self):
        result = self.run_check(DJANGO_EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
                                DJANGO_EMAIL_HOST="smtp.example.invalid", DJANGO_EMAIL_PORT="587",
                                DJANGO_EMAIL_USE_TLS="True")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_netpoa_profile_passes(self):
        result = self.run_check(**NETPOA_ENV)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("accounts.E00", result.stderr)

    def test_unsafe_combinations_fail_closed(self):
        cases = {
            "core.E002": {"DJANGO_DEBUG": "True"},
            "core.E003": {"DJANGO_SECRET_KEY": "replace-with-a-generated-secret-key"},
            "core.E004": {"DJANGO_ALLOWED_HOSTS": "*"},
            "core.E006": {"DJANGO_SECURE_SSL_REDIRECT": "False"},
            "notifications.E001": {"DJANGO_EMAIL_BACKEND": "django.core.mail.backends.console.EmailBackend"},
            "payments.E001": {"DJANGO_PAYMENTS_MODE": "live"},
            # PAY-01: real manual payments never run inside the demo or beside sandbox payments.
            "payments.E003": {"DJANGO_MANUAL_MEMBERSHIP_PAYMENTS": "True"},
            "payments.E004": {"DJANGO_MANUAL_MEMBERSHIP_PAYMENTS": "True", "DJANGO_RESTRICTED_DEMO": "False",
                              "DJANGO_PUBLIC_REGISTRATION_APPROVED": "True", "DJANGO_PAYMENTS_MODE": "sandbox",
                              "DJANGO_PAYMENTS_SANDBOX_ALLOWED": "True"},
            "accounts.E007": {**NETPOA_ENV, "DJANGO_TRUSTED_PROXY_NETWORKS": None},
            # REG-01: public production must put its own HTTPS host in emailed account links.
            "core.E009": {**NETPOA_ENV, "DJANGO_RESTRICTED_DEMO": "False", "DJANGO_PUBLIC_BASE_URL": None},
            "accounts.E005": {**NETPOA_ENV, "DJANGO_CLIENT_IP_HEADER": "CF-Connecting-IP",
                              "DJANGO_TRUSTED_PROXY_NETWORKS": None, "DJANGO_PROXY_ORIGIN_RESTRICTED": "True"},
        }
        for check_id, overrides in cases.items():
            with self.subTest(check_id):
                self.assert_fails_with(check_id, **overrides)

    def test_production_stage_one_with_registration_closed_passes(self):
        """NET-02 Stage 1: production mode, registration not yet approved, payments off."""
        result = self.run_check(**NETPOA_ENV, DJANGO_RESTRICTED_DEMO="False", DJANGO_PAYMENTS_MODE="disabled",
                                DJANGO_PUBLIC_BASE_URL="https://demo.clipcash.example",
                                DJANGO_EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
                                DJANGO_EMAIL_HOST="smtp.example.invalid", DJANGO_EMAIL_PORT="587",
                                DJANGO_EMAIL_USE_TLS="True")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("core.W003", result.stderr + result.stdout)

    def test_reviewed_production_launch_with_manual_payments_passes(self):
        """PAY-01: the production profile for real registration and Airtel Lipa payments."""
        result = self.run_check(**NETPOA_ENV, DJANGO_RESTRICTED_DEMO="False",
                                DJANGO_PUBLIC_REGISTRATION_APPROVED="True", DJANGO_MANUAL_MEMBERSHIP_PAYMENTS="True",
                                DJANGO_PAYMENTS_MODE="disabled", DJANGO_PUBLIC_BASE_URL="https://demo.clipcash.example",
                                DJANGO_EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
                                DJANGO_EMAIL_HOST="smtp.example.invalid", DJANGO_EMAIL_PORT="587",
                                DJANGO_EMAIL_USE_TLS="True")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("payments.E00", result.stderr + result.stdout)

    def test_intentional_demo_settings_still_pass(self):
        """Explicit, documented opt-ins for an isolated demo are not refused."""
        result = self.run_check(DJANGO_EMAIL_BACKEND="django.core.mail.backends.dummy.EmailBackend",
                                DJANGO_DEMO_EMAIL_VERIFICATION_OPTIONAL="True",
                                DJANGO_DEMO_ACCOUNTS="demo@example.invalid",
                                DJANGO_MEMBERSHIPS_COMPLIMENTARY_GRANTS="True")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("notifications.W001", result.stderr + result.stdout)
