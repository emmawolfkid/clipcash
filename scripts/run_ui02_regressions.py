"""Run UI regressions on an existing, idle, explicitly named PostgreSQL test DB.

This never creates/drops a database and never changes project configuration.
Django initializes the test schema from the existing migrations when necessary.
"""
import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TEST_LABELS = (
    "apps.core.tests.test_design_system",
    "apps.core.tests.test_homepage",
    "apps.core.tests.test_member_experience",
    "apps.accounts.tests.test_login",
    "apps.accounts.tests.test_password_reset",
    "apps.memberships.tests.test_web",
    "apps.administration.tests.test_dashboard",
    "apps.administration.tests.test_complimentary_console",
    "apps.notifications.tests.test_inbox",
    "apps.notifications.tests.test_events",
    "apps.payments.tests.test_web",
    "apps.wallets.tests.test_wallet_web",
    "apps.withdrawals.tests.test_web",
    "apps.viewing.tests.test_web",
    "apps.videos.tests.test_web",
    "apps.advertising.tests.test_web",
)


def main(suite_name="UI-02"):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    if not args.database.startswith("test_"):
        parser.error("Only an explicitly named test_* database is allowed.")
    os.environ["DB_TEST_NAME"] = args.database  # This process only.
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()
    from django.conf import settings
    from django.core.management import call_command
    import psycopg

    config = settings.DATABASES["default"]
    if args.database == config["NAME"]:
        parser.error("The test database must differ from the application database.")
    if settings.TEST_RUNNER != "config.test_runner.PostgreSQLTestRunner":
        parser.error("The guarded project test runner is required.")
    with psycopg.connect(dbname="postgres", user=config["USER"], password=config["PASSWORD"],
                        host=config["HOST"], port=config["PORT"], autocommit=True) as guard:
        with guard.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (args.database,))
            if not cursor.fetchone()[0]:
                parser.error("Another guarded runner already owns this test database.")
            cursor.execute("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=%s)", (args.database,))
            if not cursor.fetchone()[0]:
                parser.error("Provision a separate test database first; this script will not create one.")
            cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=%s", (args.database,))
            if cursor.fetchone()[0]:
                parser.error("The selected test database is in use; no tests were started.")
        print(f"Isolated {suite_name} regression database: {args.database}; initially idle; keepdb=True", flush=True)
        call_command("test", *TEST_LABELS, keepdb=True, interactive=False, verbosity=1)


if __name__ == "__main__":
    main()
