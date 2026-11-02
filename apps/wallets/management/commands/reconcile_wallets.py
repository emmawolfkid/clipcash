from django.core.management.base import BaseCommand

from apps.audit.context import audit_scope
from apps.wallets import wallet_services


class Command(BaseCommand):
    help = "Check member wallets against the ledger and record discrepancies (never edits history)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=1000)

    def handle(self, *args, limit, **options):
        with audit_scope(source="SYSTEM"):
            summary = wallet_services.reconcile(limit=limit)
        self.stdout.write(f"Checked {summary['wallets']} wallets; {summary['findings']} findings.")
