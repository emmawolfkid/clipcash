"""Archive audit events to a JSON Lines file (one event per line)."""

import datetime
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.audit.context import audit_scope
from apps.audit.models import AuditEvent
from apps.audit.services import SUCCESS, record_security_event

FIELDS = (
    "id", "occurred_at", "category", "action", "outcome", "source",
    "actor_id", "actor_email", "actor_role",
    "target_app_label", "target_model", "target_id", "target_repr",
    "request_id", "ip_address", "user_agent", "metadata", "idempotency_key",
)


def parse_date(value):
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as error:
        raise CommandError(f"Invalid date {value!r}; use YYYY-MM-DD.") from error


class Command(BaseCommand):
    help = "Export audit events occurring in [--since, --until) to a JSON Lines file."

    def add_arguments(self, parser):
        parser.add_argument("--since", required=True, help="Start date (inclusive), YYYY-MM-DD.")
        parser.add_argument("--until", required=True, help="End date (exclusive), YYYY-MM-DD.")
        parser.add_argument("--output", required=True, help="Path of the file to create.")

    def handle(self, *args, since, until, output, **options):
        start, end = parse_date(since), parse_date(until)
        if start >= end:
            raise CommandError("--since must be before --until.")
        path = Path(output)
        if path.exists():
            raise CommandError(f"{path} already exists; refusing to overwrite an archive.")

        tz = datetime.timezone.utc
        start_at = datetime.datetime.combine(start, datetime.time.min, tz)
        end_at = datetime.datetime.combine(end, datetime.time.min, tz)
        events = (
            AuditEvent.objects.filter(occurred_at__gte=start_at, occurred_at__lt=end_at)
            .order_by("occurred_at", "id")
            .values(*FIELDS)
        )

        count = 0
        with audit_scope(source=AuditEvent.Source.CLI):
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                for row in events.iterator(chunk_size=2000):
                    handle.write(json.dumps(row, default=str, sort_keys=True) + "\n")
                    count += 1
            record_security_event(
                "audit.exported",
                outcome=SUCCESS,
                actor=None,
                metadata={"count": count, "start": since, "end": until},
            )
        self.stdout.write(f"Exported {count} audit events to {path} at {timezone.now():%Y-%m-%d %H:%M}.")
