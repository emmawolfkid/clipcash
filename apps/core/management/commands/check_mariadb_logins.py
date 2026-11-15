"""DB-01: verify the MariaDB migration and runtime logins separately (read-only).

Configure the MIGRATION login as DB_USER and the RUNTIME login as DB_RUNTIME_USER, then run
`python manage.py check_mariadb_logins`. Exits non-zero on any finding.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from apps.core.mariadb_logins import migration_login_findings, runtime_login_findings
from apps.core.migration_ops import is_mariadb


class Command(BaseCommand):
    help = "Verify the MariaDB migration (DB_USER) and runtime (DB_RUNTIME_USER) logins."

    def handle(self, *args, **options):
        default = connections["default"]
        if not is_mariadb(default):
            raise CommandError("This check is for MariaDB (DB_ENGINE=mariadb).")
        if "runtime" not in connections:
            raise CommandError("Set DB_RUNTIME_USER and DB_RUNTIME_PASSWORD to the runtime login.")
        runtime = connections["runtime"]
        results = {
            "migration login": migration_login_findings(default, runtime),
            "runtime login": runtime_login_findings(runtime),
        }
        for login, findings in results.items():
            for finding in findings:
                self.stdout.write(f"FAIL {login}: {finding}")
            if not findings:
                self.stdout.write(f"OK   {login}")
        if any(results.values()):
            raise CommandError("MariaDB login separation is not safe.")
