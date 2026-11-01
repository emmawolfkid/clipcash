import datetime
import json
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from apps.audit import services
from apps.audit.models import AuditEvent


def old_event(days_ago):
    event = AuditEvent(
        category="AUTHENTICATION", action="auth.logout", outcome="SUCCESS", source="SYSTEM",
        occurred_at=timezone.now() - datetime.timedelta(days=days_ago),
    )
    event.save(force_insert=True)
    return event


class ExportTests(TestCase):
    def test_exports_json_lines_and_records_export(self):
        old_event(10)
        recent = services.record_security_event("auth.logout", outcome=services.SUCCESS, actor=None)
        today = timezone.now().date()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            call_command("audit_export", since=str(today - datetime.timedelta(days=1)),
                         until=str(today + datetime.timedelta(days=1)), output=str(path), stdout=StringIO())
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["id"] for row in rows], [str(recent.pk)])

            with self.assertRaises(CommandError):  # Never overwrite an archive.
                call_command("audit_export", since="2020-01-01", until="2020-01-02", output=str(path))

        event = AuditEvent.objects.get(action="audit.exported")
        self.assertEqual((event.source, event.metadata["count"]), ("CLI", 1))

    def test_invalid_dates(self):
        for since, until in (("2026-13-01", "2026-12-01"), ("2026-02-01", "2026-01-01")):
            with self.subTest(since=since), self.assertRaises(CommandError):
                call_command("audit_export", since=since, until=until, output="unused.jsonl")


class PurgeTests(TestCase):
    def test_dry_run_by_default(self):
        old_event(800)
        out = StringIO()
        cutoff = timezone.now().date() - datetime.timedelta(days=400)
        call_command("audit_purge", before=str(cutoff), stdout=out)
        self.assertIn("Dry run: 1", out.getvalue())
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_refuses_to_purge_within_minimum_retention(self):
        with self.assertRaises(CommandError):
            call_command("audit_purge", before=str(timezone.now().date()), confirm=True)

    def test_confirmed_purge_deletes_only_old_events_and_records_itself(self):
        expired = old_event(800)
        kept = old_event(100)
        cutoff = timezone.now().date() - datetime.timedelta(days=400)
        call_command("audit_purge", before=str(cutoff), confirm=True, stdout=StringIO())

        self.assertFalse(AuditEvent.objects.filter(pk=expired.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(pk=kept.pk).exists())
        purge = AuditEvent.objects.get(action="audit.purged")
        self.assertEqual((purge.metadata["count"], purge.source), (1, "CLI"))
