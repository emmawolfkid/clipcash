"""
Print queue and worker health as JSON; exit code 1 when unhealthy.

Suitable for an external monitor or a second cron entry that alerts on failure:
stale worker heartbeat, overdue backlog, expired leases or an unavailable cache.
"""

import json

from django.core.management.base import BaseCommand, CommandError

from apps.core.jobs.builtin import probe_cache
from apps.core.jobs.services import queue_health


class Command(BaseCommand):
    help = "Report background job health (exit 1 if unhealthy)."

    def add_arguments(self, parser):
        parser.add_argument("--no-cache-probe", action="store_true", help="Skip the live cache probe.")

    def handle(self, *args, no_cache_probe, **options):
        health = queue_health()
        if not no_cache_probe:
            health["cache_ok"] = probe_cache()
            if not health["cache_ok"] and "cache_unavailable" not in health["problems"]:
                health["problems"].append("cache_unavailable")
                health["healthy"] = False
        self.stdout.write(json.dumps(health, sort_keys=True))
        if not health["healthy"]:
            raise CommandError("Background processing is unhealthy: " + ", ".join(health["problems"]))
