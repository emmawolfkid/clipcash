"""Test runner that only ever uses the dedicated PostgreSQL test database."""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.test.runner import DiscoverRunner


class PostgreSQLTestRunner(DiscoverRunner):
    """
    Refuses to run unless every database is PostgreSQL and its test database is a
    distinct ``test_*`` database. After setup it confirms the live connection
    really points at that test database, so ``--keepdb`` can never reuse the
    development database.
    """

    def setup_databases(self, **kwargs):
        expected = {}
        for alias, config in settings.DATABASES.items():
            if config["ENGINE"] != "django.db.backends.postgresql":
                raise ImproperlyConfigured(
                    f"Database '{alias}' is not PostgreSQL; tests must run on PostgreSQL."
                )
            test_name = config.get("TEST", {}).get("NAME") or f"test_{config['NAME']}"
            if test_name == config["NAME"] or not test_name.startswith("test_"):
                raise ImproperlyConfigured(f"Unsafe test database name for '{alias}': {test_name!r}.")
            expected[alias] = test_name

        result = super().setup_databases(**kwargs)

        for alias, test_name in expected.items():
            with connections[alias].cursor() as cursor:
                cursor.execute("SELECT current_database()")
                (current,) = cursor.fetchone()
            if current != test_name:
                raise ImproperlyConfigured(
                    f"Expected test database {test_name!r} but connected to {current!r}."
                )

        self._restore_baseline_data()
        return result

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
