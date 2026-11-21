"""
Delete audit events older than a cut-off date, after they have been archived.

Dry run by default. The cut-off must respect AUDIT_MIN_RETENTION_DAYS. The
delete runs in one transaction that sets ``clipcash.audit_purge`` so the
append-only trigger allows it, and records an ``audit.purged`` event in the same
transaction. On MariaDB (DB-01) the flag is ``@clipcash_audit_purge`` and only the
schema owner (migration) login may purge.
"""

import datetime

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from apps.audit.context import audit_scope
from apps.audit.models import AuditEvent
from apps.audit.services import SUCCESS, record_business_event


class Command(BaseCommand):
    help = "Delete audit events that occurred before --before (dry run unless --confirm)."

    def add_arguments(self, parser):
        parser.add_argument("--before", required=True, help="Cut-off date (exclusive), YYYY-MM-DD.")
        parser.add_argument("--confirm", action="store_true", help="Actually delete.")

    def handle(self, *args, before, confirm, **options):
        try:
            cutoff_date = datetime.date.fromisoformat(before)
        except ValueError as error:
            raise CommandError(f"Invalid date {before!r}; use YYYY-MM-DD.") from error

        latest_allowed = timezone.now().date() - datetime.timedelta(days=settings.AUDIT_MIN_RETENTION_DAYS)
        if cutoff_date > latest_allowed:
            raise CommandError(
                f"Refusing: events must be kept at least {settings.AUDIT_MIN_RETENTION_DAYS} days "
                f"(latest allowed cut-off is {latest_allowed})."
            )

        cutoff = datetime.datetime.combine(cutoff_date, datetime.time.min, datetime.timezone.utc)
        count = AuditEvent.objects.filter(occurred_at__lt=cutoff).count()
        if not confirm:
            self.stdout.write(f"Dry run: {count} audit events before {cutoff_date} would be deleted.")
            return

        with audit_scope(source=AuditEvent.Source.CLI), transaction.atomic():
            with connection.cursor() as cursor:
                if connection.vendor == "mysql":
                    deleted = self._purge_mariadb(cursor, cutoff)
                else:
                    cursor.execute("SET LOCAL clipcash.audit_purge = 'on'")
                    cursor.execute("DELETE FROM audit_auditevent WHERE occurred_at < %s", [cutoff])
                    deleted = cursor.rowcount
                    cursor.execute("SET LOCAL clipcash.audit_purge = 'off'")
            record_business_event(
                "audit.purged",
                outcome=SUCCESS,
                actor=None,
                metadata={"count": deleted, "before": before},
            )
        self.stdout.write(f"Deleted {deleted} audit events before {cutoff_date}.")

    @staticmethod
    def _purge_mariadb(cursor, cutoff):
        """DB-01. MariaDB has no SET LOCAL: the flag is a session variable, which is neither
        transactional nor reset on COMMIT, so it is cleared whatever happens. The database
        also requires the schema owner (migration) login and the minimum retention
        (audit migration 0002): run this command with DB_USER set to the migration login."""
        cursor.execute("SET @clipcash_audit_purge = 'on'")
        try:
            cursor.execute("DELETE FROM audit_auditevent WHERE occurred_at < %s", [cutoff])
            return cursor.rowcount
        finally:
            cursor.execute("SET @clipcash_audit_purge = NULL")
