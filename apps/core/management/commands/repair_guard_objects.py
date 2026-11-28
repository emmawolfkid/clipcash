"""
DB-01 deployment repair (MariaDB): install the core.0004 objects that a database migrated before
2026-11-15 never received, then verify every ClipCash guard trigger.

Background: the MariaDB section of core.0004 (the clipcash_guard_triggers view, the
core_transaction_lock table and the three authorization_permissions_* triggers on
auth_group_permissions) was added after that migration had already been applied to some databases.
Django does not re-run an applied migration, so those databases lack the objects.

* Preview by default; nothing changes without --apply.
* The statements come from the migration itself (the authoritative definitions); each one runs only
  when its object is missing, so the command is idempotent and never drops or recreates anything.
* Migration history (django_migrations) is never touched.
* Run it with the migration login (it needs CREATE VIEW, CREATE and TRIGGER), after a verified
  backup, then confirm "guard findings: none".

    python manage.py repair_guard_objects            # preview and verify
    python manage.py repair_guard_objects --apply    # create what is missing, then verify
"""

import re
from importlib import import_module

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from apps.core.mariadb_guards import expected_guard_triggers, guard_findings

MIGRATION = "apps.core.migrations.0004_pin_function_search_path"
OBJECT = re.compile(r"CREATE (?:SQL SECURITY DEFINER )?(VIEW|TABLE|TRIGGER) (\w+)")


def _exists(cursor, kind, name):
    if kind == "TRIGGER":
        cursor.execute("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE() "
                       "AND TRIGGER_NAME = %s", [name])
    else:
        cursor.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
                       "AND TABLE_NAME = %s", [name])
    return cursor.fetchone()[0] > 0


class Command(BaseCommand):
    help = "Preview (default) or --apply the DB-01 repair of missing core.0004 MariaDB objects, then verify guards."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Create the missing objects shown in the preview.")

    def handle(self, *args, apply, **options):
        if connection.vendor != "mysql":
            self.stdout.write("Not a MariaDB database: nothing to repair (PostgreSQL guards are separate).")
            return
        statements = import_module(MIGRATION).MARIADB_FORWARD
        missing = []
        with connection.cursor() as cursor:
            for statement in statements:
                match = OBJECT.search(statement)
                if match is None:
                    raise CommandError("Unrecognised statement in core.0004; refusing to guess.")
                kind, name = match.groups()
                present = _exists(cursor, kind, name)
                self.stdout.write(f"{'present' if present else 'MISSING'}  {kind} {name}")
                if not present:
                    missing.append((kind, name, statement))
            if missing and apply:
                for kind, name, statement in missing:  # Migration order: view and table before triggers.
                    cursor.execute(statement)
                    self.stdout.write(self.style.SUCCESS(f"created  {kind} {name}"))
            cursor.execute("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()")
            installed = cursor.fetchone()[0]
        self.stdout.write(f"triggers installed: {installed}; expected by the migrations: {len(expected_guard_triggers())}")
        if missing and not apply:
            self.stdout.write("\nPreview only. Take a verified backup, then run with --apply as the migration login.")
        findings = guard_findings(connection)
        self.stdout.write("guard findings: " + ("; ".join(findings) if findings else "none"))
        if findings and (apply or not missing):
            raise CommandError("Guard verification failed: " + "; ".join(findings))
