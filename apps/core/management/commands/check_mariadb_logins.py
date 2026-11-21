"""DB-01: verify the MariaDB migration and runtime logins separately (read-only).

Configure the MIGRATION login as DB_USER and the RUNTIME login as DB_RUNTIME_USER, then run
`python manage.py check_mariadb_logins`. Exits non-zero on any finding.

PRD-00B: it also confirms that every guard trigger created by ClipCash's migrations is installed
(and nothing else), and that the money-lock constraints exist. Before the first `migrate` of an
empty database, use `--before-migrate` to check only the logins.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from apps.core.mariadb_guards import expected_guard_triggers, guard_findings
from apps.core.mariadb_logins import migration_login_findings, runtime_login_findings
from apps.core.migration_ops import is_mariadb


class Command(BaseCommand):
    help = "Verify the MariaDB migration (DB_USER) and runtime (DB_RUNTIME_USER) logins and the guard triggers."

    def add_arguments(self, parser):
        parser.add_argument("--before-migrate", action="store_true",
                            help="Check only the logins (an empty database has no guards yet).")

    def handle(self, *args, before_migrate=False, **options):
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
        if not before_migrate:
            results["guards"] = guard_findings(default)
        for subject, findings in results.items():
            for finding in findings:
                self.stdout.write(f"FAIL {subject}: {finding}")
            if not findings:
                detail = f" ({len(expected_guard_triggers())} triggers and the lock constraints)" if subject == "guards" else ""
                self.stdout.write(f"OK   {subject}{detail}")
        if any(results.values()):
            raise CommandError("MariaDB login separation or guards are not safe.")
