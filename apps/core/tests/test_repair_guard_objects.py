"""DB-01 deployment repair command (MariaDB): preview changes nothing; --apply recreates exactly the
missing core.0004 objects from the migration's own statements, and is idempotent."""

from io import StringIO
from unittest import skipUnless

from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase

TRIGGER = "authorization_permissions_delete"


def trigger_exists():
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE() "
                       "AND TRIGGER_NAME = %s", [TRIGGER])
        return cursor.fetchone()[0] == 1


@skipUnless(connection.vendor == "mysql", "MariaDB-only repair.")
class RepairGuardObjectsTests(TransactionTestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command("repair_guard_objects", *args, stdout=out)
        return out.getvalue()

    def test_preview_then_apply_restores_a_missing_trigger(self):
        self.assertIn("guard findings: none", self.run_command())
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TRIGGER {TRIGGER}")  # Simulate a database migrated before 2026-11-15.
        try:
            preview = self.run_command()
            self.assertIn(f"MISSING  TRIGGER {TRIGGER}", preview)
            self.assertIn("Preview only", preview)
            self.assertFalse(trigger_exists())
            applied = self.run_command("--apply")
            self.assertIn(f"created  TRIGGER {TRIGGER}", applied)
            self.assertIn("guard findings: none", applied)
            self.assertTrue(trigger_exists())
            again = self.run_command("--apply")
            self.assertNotIn("created", again)  # Idempotent.
        finally:
            if not trigger_exists():
                self.run_command("--apply")
