"""
PRD-00B: MariaDB production readiness: backup and restore scripts, deployment templates and the
migration-login checks. The scripts' database behaviour is proven by the drill recorded in
docs/deployment/netpoa_mariadb_runbook.md (disposable databases only); these tests cover the
safety refusals, which happen before any database is contacted, and the static guarantees.
"""

import os
import subprocess
import tempfile
from pathlib import Path
from unittest import skipUnless

from django.conf import settings
from django.db import connection
from django.test import SimpleTestCase, TestCase

from apps.core.mariadb_guards import expected_guard_triggers, guard_findings
from apps.core.mariadb_logins import ALL_PRIVILEGES, global_privileges
from apps.core.migration_ops import is_mariadb
from apps.core.tests.test_deploy_safety import NETPOA_ENV
from apps.core.tests.test_deployment import run_python
from apps.core.tests.test_netpoa_deployment import usable_bash

ROOT = Path(settings.BASE_DIR)
SCRIPTS = ROOT / "scripts" / "database" / "mariadb"
DEPLOYMENT = ROOT / "docs" / "deployment"


class ScriptSafetyTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bash = usable_bash()

    def setUp(self):
        if not self.bash:
            self.skipTest("bash is not available")

    def run_script(self, name, *args, **env):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("CLIPCASH_")}
        environment.update(env)
        return subprocess.run([self.bash, str(SCRIPTS / name), *args], capture_output=True, text=True, timeout=60,
                              env=environment)

    def test_scripts_parse(self):
        for name in ("backup.sh", "restore_check.sh"):
            with self.subTest(name):
                result = subprocess.run([self.bash, "-n", str(SCRIPTS / name)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_usage_without_arguments(self):
        for name in ("backup.sh", "restore_check.sh"):
            with self.subTest(name):
                result = self.run_script(name)
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage", result.stderr)

    def test_backup_refuses_a_directory_inside_the_repository_and_an_open_option_file(self):
        with tempfile.TemporaryDirectory() as outside:
            option = Path(outside) / "backup.cnf"
            option.write_text("[client]\n", encoding="utf-8")
            inside = self.run_script("backup.sh", str(ROOT / "backups-must-not-exist"), CLIPCASH_DB_OPTION_FILE=str(option),
                                     CLIPCASH_DB_NAME="clipcash", CLIPCASH_ALLOW_OPEN_OPTION_FILE="1")
            self.assertEqual(inside.returncode, 2)
            self.assertIn("inside the repository", inside.stderr)
            self.assertFalse((ROOT / "backups-must-not-exist").exists())
            if os.name != "nt":  # File modes are meaningful only on POSIX hosts (NetPoa).
                option.chmod(0o644)
                open_file = self.run_script("backup.sh", str(Path(outside) / "out"), CLIPCASH_DB_OPTION_FILE=str(option),
                                            CLIPCASH_DB_NAME="clipcash")
                self.assertEqual(open_file.returncode, 2)
                self.assertIn("chmod 600", open_file.stderr)
        repo_option = self.run_script("backup.sh", tempfile.gettempdir(), CLIPCASH_DB_OPTION_FILE=str(ROOT / "manage.py"),
                                      CLIPCASH_DB_NAME="clipcash", CLIPCASH_ALLOW_OPEN_OPTION_FILE="1")
        self.assertEqual(repo_option.returncode, 2)
        self.assertIn("option file is inside the repository", repo_option.stderr)

    def test_restore_refuses_any_target_that_could_be_a_real_database(self):
        with tempfile.TemporaryDirectory() as outside:
            dump = Path(outside) / "clipcash-x.sql.gz"
            dump.write_bytes(b"")
            option = Path(outside) / "restore.cnf"
            option.write_text("[client]\n", encoding="utf-8")
            base = {"CLIPCASH_DB_OPTION_FILE": str(option), "CLIPCASH_ALLOW_OPEN_OPTION_FILE": "1"}
            cases = {
                "clipcash": "does not contain 'restore'",
                "acct_clipcash": "does not contain 'restore'",
                "bad-name;drop": "only letters, digits and underscores",
            }
            for target, message in cases.items():
                with self.subTest(target):
                    result = self.run_script("restore_check.sh", str(dump), CLIPCASH_RESTORE_DB=target, **base)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn(message, result.stderr)
            same = self.run_script("restore_check.sh", str(dump), CLIPCASH_RESTORE_DB="acct_clipcash_restore",
                                   CLIPCASH_PRODUCTION_DB="acct_clipcash_restore", **base)
            self.assertEqual(same.returncode, 2)
            self.assertIn("is the production database", same.stderr)
            unverified = self.run_script("restore_check.sh", str(dump), CLIPCASH_RESTORE_DB="acct_clipcash_restore",
                                         **base)
            self.assertEqual(unverified.returncode, 2)
            self.assertIn("cannot be verified", unverified.stderr)

    def test_scripts_keep_credentials_out_of_command_lines(self):
        for name in ("backup.sh", "restore_check.sh"):
            text = (SCRIPTS / name).read_text(encoding="utf-8")
            with self.subTest(name):
                self.assertIn('--defaults-extra-file="$option_file"', text)
                self.assertNotRegex(text, r"--password|\s-p\S|MYSQL_PWD|DB_PASSWORD")
        self.assertIn("umask 077", (SCRIPTS / "backup.sh").read_text(encoding="utf-8"))

    def test_backup_never_writes_drop_statements_and_restore_rewrites_only_definers(self):
        backup = (SCRIPTS / "backup.sh").read_text(encoding="utf-8")
        self.assertIn("--single-transaction", backup)
        self.assertIn("--skip-add-drop-table", backup)
        self.assertIn("--triggers", backup)
        restore = (SCRIPTS / "restore_check.sh").read_text(encoding="utf-8")
        # Only versioned-comment lines (trigger and view headers) are touched, never row data.
        self.assertIn(r"sed -E '/^\/\*!50[0-9]{3} /s/DEFINER=`[^`]*`@`[^`]*`//'", restore)


class DeploymentTemplateTests(SimpleTestCase):
    @staticmethod
    def active(path):
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")]

    def test_production_template_uses_mariadb_and_the_runtime_login(self):
        lines = self.active(DEPLOYMENT / "netpoa_production.env.example")
        self.assertIn("DB_ENGINE=mariadb", lines)
        self.assertIn("DB_USER=<account>_ccapp", lines)
        self.assertFalse([line for line in lines if line.startswith(("DATABASE_URL", "DJANGO_DB_SSLMODE"))])
        self.assertIn("DJANGO_EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend", lines)
        self.assertIn("DJANGO_PAYMENTS_MODE=disabled", lines)

    def test_migration_and_backup_templates_hold_placeholders_only(self):
        migrate = self.active(DEPLOYMENT / "netpoa_migrate.env.example")
        self.assertIn("DB_USER=<account>_ccmigrate", migrate)
        client = self.active(DEPLOYMENT / "netpoa_mariadb_client.cnf.example")
        self.assertIn("user=<account>_ccmigrate", client)
        for line in migrate + client:
            if "=" in line:
                self.assertRegex(line.split("=", 1)[1], r"^(<[^>]+>\w*|localhost|\d+)$", line)


try:
    import MySQLdb  # noqa: F401
    HAVE_DRIVER = True
except ImportError:
    HAVE_DRIVER = False

# The NetPoa MariaDB profile of netpoa_production.env.example, with fake values (.invalid hosts never resolve).
MARIADB_PRODUCTION = {
    **NETPOA_ENV, "DATABASE_URL": None, "DJANGO_DB_SSLMODE": None, "DB_ENGINE": "mariadb",
    "DB_NAME": "acct_clipcash", "DB_USER": "acct_ccapp", "DB_PASSWORD": "example-runtime-password",
    "DB_HOST": "localhost", "DB_PORT": "3306", "DB_TEST_NAME": "test_acct_clipcash", "DB_RUNTIME_USER": None,
    "DJANGO_RESTRICTED_DEMO": "False", "DJANGO_PAYMENTS_MODE": "disabled",
    "DJANGO_PUBLIC_BASE_URL": "https://demo.clipcash.example",
    "DJANGO_EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend", "DJANGO_EMAIL_HOST": "smtp.example.invalid",
    "DJANGO_EMAIL_PORT": "587", "DJANGO_EMAIL_USE_TLS": "True",
    "DJANGO_DEFAULT_FROM_EMAIL": "ClipCash <no-reply@demo.clipcash.example>",
}


@skipUnless(HAVE_DRIVER, "mysqlclient is not installed (requirements/mariadb.txt).")
class MariaDBProductionProfileTests(SimpleTestCase):
    def check(self, **overrides):
        return run_python(["manage.py", "check", "--deploy", "--fail-level", "ERROR"], **{**MARIADB_PRODUCTION, **overrides})

    def test_the_netpoa_mariadb_profile_passes_the_deploy_checks(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("core.W003", result.stderr + result.stdout)  # Stage 1: registration closed.

    def test_postgresql_only_settings_are_refused_with_mariadb(self):
        for name, value in (("DATABASE_URL", "postgres://a:b@db.invalid:5432/c"), ("DJANGO_DB_SSLMODE", "require")):
            with self.subTest(name):
                result = self.check(**{name: value})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ImproperlyConfigured", result.stderr)


class GlobalPrivilegeTests(SimpleTestCase):
    def test_database_level_all_is_not_super(self):
        """cPanel grants ALL PRIVILEGES on the database only: that cannot create triggers while
        binary logging is on, so it must not satisfy the binlog check."""
        lines = ["GRANT USAGE ON *.* TO `acct_ccmigrate`@`localhost`",
                 "GRANT ALL PRIVILEGES ON `acct\\_clipcash`.* TO `acct_ccmigrate`@`localhost`"]
        self.assertEqual(global_privileges(lines), {"USAGE"})

    def test_global_super_or_all_counts(self):
        self.assertIn("SUPER", global_privileges(["GRANT SELECT, SUPER ON *.* TO `x`@`%`"]))
        self.assertIn(ALL_PRIVILEGES, global_privileges(["GRANT ALL PRIVILEGES ON *.* TO `root`@`localhost` "
                                                         "WITH GRANT OPTION"]))


@skipUnless(is_mariadb(connection), "MariaDB only.")
class GuardInventoryTests(TestCase):
    databases = {"default"}

    def test_every_migrated_guard_is_installed(self):
        """What check_mariadb_logins now reports as "guards" (the command itself was run in the
        PRD-00B drill against a restored copy; see docs/deployment/netpoa_mariadb_runbook.md)."""
        self.assertGreaterEqual(len(expected_guard_triggers()), 108)
        self.assertEqual(guard_findings(connection), [])
