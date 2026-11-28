"""Test runner that only ever uses the dedicated PostgreSQL (or, DB-01, MariaDB) test database."""

import os

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.test.runner import DiscoverRunner


# Tests only: a fast hasher. Production keeps Django's default (PBKDF2, 1,000,000
# iterations, ~0.9 s per hash), which made user-heavy test setup dominate the
# suite's runtime. Hash strength is not under test; authentication behaviour is.
TEST_PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# DB-01: PostgreSQL, or ClipCash's own MariaDB backend (never the stock MySQL backend,
# which would silently skip ClipCash's partial and case-insensitive unique rules).
# The query names the database the live connection really uses.
SUPPORTED_ENGINES = {
    "django.db.backends.postgresql": "SELECT current_database()",
    "config.db_backends.mariadb": "SELECT DATABASE()",
}
RUNTIME_LOGIN_ENV = "CLIPCASH_TEST_AS_RUNTIME"


class PostgreSQLTestRunner(DiscoverRunner):
    """
    Refuses to run unless every database is PostgreSQL (or ClipCash's MariaDB
    backend, DB-01; the class keeps its name for existing references) and its test database is a
    distinct ``test_*`` database. After setup it confirms the live connection
    really points at that test database, so ``--keepdb`` can never reuse the
    development database.
    """

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        settings.PASSWORD_HASHERS = TEST_PASSWORD_HASHERS
        # OPS-01: automated tests are an isolated environment: the testing tools stay listed, and pages
        # follow normal language negotiation unless a test switches the Swahili console default on.
        settings.SIMULATION_TOOLS_VISIBLE = True
        settings.CONSOLE_DEFAULT_LANGUAGE = ""

    def setup_databases(self, **kwargs):
        expected = {}
        for alias, config in settings.DATABASES.items():
            if config["ENGINE"] not in SUPPORTED_ENGINES:
                raise ImproperlyConfigured(
                    f"Database '{alias}' is not PostgreSQL; tests must run on PostgreSQL "
                    "(or ClipCash's MariaDB backend)."
                )
            test_name = config.get("TEST", {}).get("NAME") or f"test_{config['NAME']}"
            if test_name == config["NAME"] or not test_name.startswith("test_"):
                raise ImproperlyConfigured(f"Unsafe test database name for '{alias}': {test_name!r}.")
            expected[alias] = test_name

        result = super().setup_databases(**kwargs)

        # Django sets up only the aliases the selected tests use (``aliases``; None means
        # all). A run of only SimpleTestCase classes sets up none, so every connection still
        # names the development database. Verify every alias that was set up, and touch no
        # database (baseline rows, runtime login) unless "default" really is the test database.
        aliases = kwargs.get("aliases")
        prepared = set(expected) if aliases is None else set(expected) & set(aliases)
        for alias in prepared:
            test_name = expected[alias]
            with connections[alias].cursor() as cursor:
                cursor.execute(SUPPORTED_ENGINES[settings.DATABASES[alias]["ENGINE"]])
                (current,) = cursor.fetchone()
            if current != test_name:
                raise ImproperlyConfigured(
                    f"Expected test database {test_name!r} but connected to {current!r}."
                )

        if "default" not in prepared:
            return result
        self._restore_baseline_data()
        if os.environ.get(RUNTIME_LOGIN_ENV) == "1":
            self._run_as_runtime_login()
        return result

    def teardown_databases(self, old_config, **kwargs):
        if self._migration_credentials is not None:  # Drop the test database as its owner again.
            connection = connections["default"]
            connection.close()
            connection.settings_dict["USER"], connection.settings_dict["PASSWORD"] = self._migration_credentials
            self._migration_credentials = None
        super().teardown_databases(old_config, **kwargs)

    _migration_credentials = None

    def _run_as_runtime_login(self):
        """DB-01 (MariaDB, opt-in with CLIPCASH_TEST_AS_RUNTIME=1): the migration login creates and
        migrates the test database as usual, then every test runs as the least-privileged RUNTIME
        login (DB_RUNTIME_USER), so service paths and raw-SQL attempts meet the guards with the
        application's own rights. Only TestCase classes fit: flushing (TransactionTestCase) needs
        TRUNCATE, which the runtime login must not hold. Teardown switches back to the owner."""
        runtime = settings.DATABASES.get("runtime")
        if runtime is None or settings.DATABASES["default"]["ENGINE"] != "config.db_backends.mariadb":
            raise ImproperlyConfigured(f"{RUNTIME_LOGIN_ENV}=1 needs MariaDB and DB_RUNTIME_USER.")
        connection = connections["default"]
        self._migration_credentials = (connection.settings_dict["USER"], connection.settings_dict["PASSWORD"])
        connection.close()
        connection.settings_dict["USER"], connection.settings_dict["PASSWORD"] = runtime["USER"], runtime["PASSWORD"]
        with connection.cursor() as cursor:
            cursor.execute("SELECT CURRENT_USER()")
            (login,) = cursor.fetchone()
        if login.rsplit("@", 1)[0] != runtime["USER"]:
            raise ImproperlyConfigured(f"Expected to run as {runtime['USER']!r} but connected as {login!r}.")
        print(f"Running tests as the runtime login {login}.")

    @staticmethod
    def _restore_baseline_data():
        """
        Re-create rows that data migrations normally provide. With --keepdb the
        migrations do not run again, and TransactionTestCase flushes every table
        at the end of a run, so without this a second run would start without
        them. Idempotent; runs only after the database identity check above.
        """
        from django.apps import apps

        from apps.accounts.roles import ROLE_GROUP_NAMES
        from apps.core.models import SITE_CONFIGURATION_ID

        Group = apps.get_model("auth", "Group")
        for name in ROLE_GROUP_NAMES.values():
            Group.objects.get_or_create(name=name)
        apps.get_model("core", "SiteConfiguration").objects.get_or_create(id=SITE_CONFIGURATION_ID)

        # Platform ledger accounts (chart of accounts only; never entries).
        import importlib

        importlib.import_module("apps.wallets.migrations.0003_platform_accounts").create_platform_accounts(
            apps, None
        )
