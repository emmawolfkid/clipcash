"""
NetPoa (cPanel + Passenger) deployment files (docs/deployment/netpoa_runbook.md).

Nothing here connects to a database or a remote host: the Passenger entry point is
loaded in a subprocess with fake, nonsecret settings (as in test_deployment), the
cron scripts are only syntax-checked and asked to refuse bad input before they do
anything, and the SQL scripts are compared as text.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from apps.core.tests.test_deployment import ENV_PREFIXES, PRODUCTION_ENV

ROOT = Path(settings.BASE_DIR)
PASSENGER = ROOT / "passenger_wsgi.py"
NETPOA_SCRIPTS = ROOT / "scripts" / "netpoa"
CPANEL_SQL = ROOT / "scripts" / "database" / "cpanel"
RENDER_GRANTS = ROOT / "scripts" / "database" / "roles" / "03_grant_runtime.sql"

PASSENGER_PROBE = """
import runpy, sys
namespace = runpy.run_path(sys.argv[1])
from config.wsgi import application
import django.conf
print(namespace["application"] is application, callable(application),
      django.conf.settings.SETTINGS_MODULE, django.conf.settings.DEBUG)
"""


def clean_environment(**overrides):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(ENV_PREFIXES) and key not in ("PYTHONPATH", "DJANGO_SETTINGS_MODULE")}
    env.update(PRODUCTION_ENV)
    env.pop("RENDER_EXTERNAL_HOSTNAME", None)  # Not on Render.
    env.update(DJANGO_ALLOWED_HOSTS="clipcash.example.invalid", DJANGO_TRUSTED_PROXY_COUNT="1",
               DJANGO_SECURE_PROXY_SSL_HEADER="False", **overrides)
    return env


class PassengerEntryPointTests(SimpleTestCase):
    def run_probe(self, **overrides):
        with tempfile.TemporaryDirectory() as elsewhere:  # Passenger may start outside the app root.
            return subprocess.run([sys.executable, "-c", PASSENGER_PROBE, str(PASSENGER)], cwd=elsewhere,
                                  env=clean_environment(**overrides), capture_output=True, text=True, timeout=120)

    def test_exposes_the_project_wsgi_application_from_any_directory(self):
        result = self.run_probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["True", "True", "config.settings", "False"])

    def test_an_explicit_settings_module_is_kept(self):
        result = self.run_probe(DJANGO_SETTINGS_MODULE="apps.core.tests.no_such_settings")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no_such_settings", result.stderr)

    def test_holds_no_host_specific_values(self):
        source = PASSENGER.read_text(encoding="utf-8")
        for forbidden in ("/home/", "public_html", "PASSWORD", "SECRET_KEY", "DATABASE_URL", ".onrender.com",
                          "environ[\"DJANGO_"):
            self.assertNotIn(forbidden, source)
        self.assertIn('os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")', source)
        self.assertIn("from config.wsgi import application", source)


def usable_bash():
    bash = shutil.which("bash")
    if not bash:
        return None
    try:
        probe = subprocess.run([bash, "-c", "echo ok"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return bash if probe.returncode == 0 and probe.stdout.strip() == "ok" else None


class CronScriptTests(SimpleTestCase):
    scripts = ("run_jobs_cron.sh", "jobs_health_cron.sh", "check_host.sh")

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bash = usable_bash()

    def require_bash(self):
        if not self.bash:
            self.skipTest("bash is not available")

    def run_script(self, name, *args, **env):
        environment = {**os.environ, **env}
        return subprocess.run([self.bash, str(NETPOA_SCRIPTS / name), *args], capture_output=True, text=True,
                              timeout=60, env=environment)

    def test_scripts_parse(self):
        self.require_bash()
        for name in self.scripts:
            with self.subTest(name):
                result = subprocess.run([self.bash, "-n", str(NETPOA_SCRIPTS / name)], capture_output=True,
                                        text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_cron_scripts_refuse_to_run_without_the_virtualenv_python(self):
        self.require_bash()
        for name in ("run_jobs_cron.sh", "jobs_health_cron.sh"):
            with self.subTest(name):
                result = self.run_script(name)
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage", result.stderr)

    def test_run_window_must_fit_a_one_minute_cron(self):
        self.require_bash()
        python = Path(sys.executable).as_posix()
        with tempfile.TemporaryDirectory() as logs:
            for value in ("120", "0", "abc", "56"):
                with self.subTest(value):
                    result = self.run_script("run_jobs_cron.sh", python, CLIPCASH_JOBS_MAX_SECONDS=value,
                                             CLIPCASH_LOG_DIR=logs)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("CLIPCASH_JOBS_MAX_SECONDS", result.stderr)
            self.assertEqual(os.listdir(logs), [])  # Refused before writing anything.

    def test_run_jobs_is_bounded_locked_private_and_secret_free(self):
        source = (NETPOA_SCRIPTS / "run_jobs_cron.sh").read_text(encoding="utf-8")
        self.assertIn('manage.py run_jobs --max-seconds "$max_seconds"', source)
        self.assertIn('max_seconds="${CLIPCASH_JOBS_MAX_SECONDS:-45}"', source)
        self.assertIn("flock -n 9", source)
        self.assertIn("umask 077", source)
        for name in ("run_jobs_cron.sh", "jobs_health_cron.sh"):
            text = (NETPOA_SCRIPTS / name).read_text(encoding="utf-8")
            for forbidden in ("DATABASE_URL", "PASSWORD", "SECRET_KEY", "public_html"):
                self.assertNotIn(forbidden, text, name)

    def test_health_check_is_silent_when_healthy(self):
        source = (NETPOA_SCRIPTS / "jobs_health_cron.sh").read_text(encoding="utf-8")
        self.assertIn("manage.py jobs_status", source)
        healthy_branch = source.split('if [ "$status" -ne 0 ]; then', 1)[1].split("fi", 1)[1]
        self.assertNotIn("echo", healthy_branch)

    def test_host_probe_changes_nothing(self):
        source = (NETPOA_SCRIPTS / "check_host.sh").read_text(encoding="utf-8")
        for forbidden in ("pip install", "crontab -e", "crontab -", "> ~", ">>", "psql \"", "psql -"):
            self.assertNotIn(forbidden, source)
        # No file-changing command at the start of a command (as a word, not inside e.g. "uname -srm").
        self.assertNotRegex(source, r"(?m)(^|[;&|(]\s*)(rm|mkdir|touch|mv|cp|chmod|tee)\s")


def unnest_lists(sql):
    return [re.findall(r"'([^']+)'", body) for body in re.findall(r"unnest\(ARRAY\[(.*?)\]\)", sql, re.S)]


class CpanelDatabaseScriptTests(SimpleTestCase):
    def read(self, name):
        return (CPANEL_SQL / name).read_text(encoding="utf-8")

    def statements(self, name):
        """The script without its comment lines (comments may name the Render roles)."""
        return "\n".join(line for line in self.read(name).splitlines() if not line.lstrip().startswith("--"))

    def test_runtime_restrictions_match_the_render_scripts(self):
        render = RENDER_GRANTS.read_text(encoding="utf-8")
        cpanel = self.read("02_grant_runtime.sql")
        self.assertEqual(unnest_lists(cpanel), unnest_lists(render))
        self.assertEqual(len(unnest_lists(cpanel)), 2)  # Append-only list and locked-insert list.
        column_grant = r"GRANT UPDATE \(([^)]*)\) ON TABLE public\.wallets_ledgeraccount"
        self.assertEqual(re.findall(column_grant, cpanel), re.findall(column_grant, render))
        trigger_rule = re.search(r"WHERE n\.nspname = 'public' AND NOT t\.tgisinternal.*?\\gexec", render, re.S)
        self.assertIn(trigger_rule.group(0), cpanel)
        for privilege in ("REVOKE TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES", "REVOKE DELETE ON TABLE public.django_migrations"):
            self.assertIn(privilege, cpanel)

    def test_role_names_are_parameters_not_fixed_names(self):
        for name in ("01_prepare_database.sql", "02_grant_runtime.sql", "03_verify_separation.sql", "00_inspect.sql"):
            with self.subTest(name):
                text = self.statements(name)
                self.assertNotRegex(text, r"\bclipcash_(app|owner|migrator)\b")
                self.assertNotRegex(text, r"(?i)password\s*'")
        for name in ("01_prepare_database.sql", "02_grant_runtime.sql"):
            self.assertIn("\\if :{?runtime_role}", self.read(name))

    def test_prepare_refuses_unsafe_logins_before_changing_anything(self):
        text = self.read("01_prepare_database.sql")
        changes = text.index("BEGIN;")
        for guard in ("version_ok", "runtime_exists", "same_login", "is_owner", "runtime_is_owner", "runtime_powerful"):
            self.assertLess(text.index(f"\\if :{guard}"), changes, guard)
        self.assertIn("REVOKE CREATE, TEMPORARY ON DATABASE %I FROM PUBLIC", text)
        self.assertIn("REVOKE ALL ON DATABASE %I FROM %I", text)

    def test_inspection_and_verification_are_read_only(self):
        for name in ("00_inspect.sql", "03_verify_separation.sql"):
            with self.subTest(name):
                text = self.read(name)
                self.assertIn("SET default_transaction_read_only = on;", text)
                self.assertNotRegex(text, r"(?im)^\s*(GRANT|REVOKE|ALTER|CREATE|DROP|INSERT|UPDATE|DELETE)\b")

    def test_verification_covers_what_cpanel_grants_by_default(self):
        text = self.read("03_verify_separation.sql")
        for check in ("has_database_privilege(current_database(), 'CREATE')",
                      "has_database_privilege(current_database(), 'TEMPORARY')",
                      "has_schema_privilege('public', 'CREATE')",
                      "pg_has_role(current_user, d.datdba, 'MEMBER')",
                      "pg_has_role(current_user, tableowner, 'MEMBER')",
                      "server_version_num')::int >= 150000"):
            self.assertIn(check, text)
        self.assertIn("RAISE EXCEPTION", text)
