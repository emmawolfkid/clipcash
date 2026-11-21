"""config.test_runner: the database identity check covers exactly the databases Django set up."""

from types import SimpleNamespace
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config.test_runner import PostgreSQLTestRunner

ENGINE = "config.db_backends.mariadb"
DATABASES = {
    "default": {"ENGINE": ENGINE, "NAME": "clipcash", "TEST": {"NAME": "test_clipcash"}},
    "runtime": {"ENGINE": ENGINE, "NAME": "clipcash", "TEST": {"NAME": "test_clipcash", "MIRROR": "default"}},
}


class SetupDatabasesTests(SimpleTestCase):
    """Django's own setup and every live connection are mocked: no real database is touched."""

    def setUp(self):
        self.cursors = {}
        handler = mock.MagicMock()
        handler.__getitem__.side_effect = self._connection
        patches = [
            mock.patch("config.test_runner.settings", SimpleNamespace(DATABASES=DATABASES)),
            mock.patch("config.test_runner.connections", handler),
            mock.patch("django.test.runner.DiscoverRunner.setup_databases", return_value="old-config"),
            mock.patch.object(PostgreSQLTestRunner, "_restore_baseline_data"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.runner = PostgreSQLTestRunner(verbosity=0)

    def _connection(self, alias):
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (self.current_names[alias],)
        self.cursors[alias] = cursor
        return connection

    def test_no_database_set_up_touches_no_database(self):
        """Regression: a run of only SimpleTestCase classes (aliases=set()) creates no test
        database, so every connection still names the development database. The runner used
        to check (and then write baseline rows to) it anyway and failed with
        "Expected test database 'test_clipcash' but connected to 'clipcash'"."""
        self.current_names = {"default": "clipcash", "runtime": "clipcash"}
        self.assertEqual(self.runner.setup_databases(aliases=set()), "old-config")
        self.assertEqual(self.cursors, {})
        PostgreSQLTestRunner._restore_baseline_data.assert_not_called()

    def test_set_up_database_still_pointing_at_development_database_is_refused(self):
        self.current_names = {"default": "clipcash", "runtime": "clipcash"}
        with self.assertRaisesMessage(ImproperlyConfigured, "Expected test database 'test_clipcash'"):
            self.runner.setup_databases(aliases={"default"})
        PostgreSQLTestRunner._restore_baseline_data.assert_not_called()

    def test_mirror_alias_is_checked_when_set_up(self):
        self.current_names = {"default": "test_clipcash", "runtime": "clipcash"}
        with self.assertRaisesMessage(ImproperlyConfigured, "connected to 'clipcash'"):
            self.runner.setup_databases(aliases={"default", "runtime"})

    def test_all_aliases_checked_when_aliases_not_given(self):
        self.current_names = {"default": "test_clipcash", "runtime": "test_clipcash"}
        self.runner.setup_databases()
        self.assertEqual(set(self.cursors), {"default", "runtime"})
        PostgreSQLTestRunner._restore_baseline_data.assert_called_once()

    def test_verified_test_database_gets_baseline_data(self):
        self.current_names = {"default": "test_clipcash", "runtime": "clipcash"}
        self.runner.setup_databases(aliases={"default"})
        self.assertEqual(set(self.cursors), {"default"})
        PostgreSQLTestRunner._restore_baseline_data.assert_called_once()

    def test_unsafe_test_database_name_is_refused_before_setup(self):
        unsafe = {"default": {"ENGINE": ENGINE, "NAME": "clipcash", "TEST": {"NAME": "clipcash"}}}
        with mock.patch("config.test_runner.settings", SimpleNamespace(DATABASES=unsafe)):
            with self.assertRaisesMessage(ImproperlyConfigured, "Unsafe test database name"):
                self.runner.setup_databases(aliases=set())
