"""
DB-01: are ClipCash's MariaDB guards installed? (the counterpart of PostgreSQL's pg_trigger checks)

The expected guards are exactly the triggers the MariaDB migration operations create, so the
list cannot drift from the migrations. The installed ones are read through the SQL SECURITY
DEFINER view clipcash_guard_triggers (core migration 0004): MariaDB shows a trigger in
information_schema only to logins holding TRIGGER on its table, which the runtime login must
not, so without the view the application's own login would see no guard at all.

Read-only; safe in a request or a test transaction (a failed read is contained in a savepoint).
"""

import re
from functools import lru_cache

from django.db import DatabaseError, transaction

from .migration_ops import MariaDBRunSQL

INVENTORY_VIEW = "clipcash_guard_triggers"
# Constraints that lock real money regardless of the triggers (also checked by demo_readiness).
# FIN-01: adv_campaign_funding_evidence replaced adv_campaign_funding_locked (funding only with ledger evidence).
LOCK_CONSTRAINTS = ("adv_campaign_funding_evidence", "sub_activation_source_consistent")
_SHOWN = 6


@lru_cache(maxsize=1)
def expected_guard_triggers():
    from django.db.migrations.loader import MigrationLoader

    names = set()
    for migration in MigrationLoader(None, ignore_no_migrations=True).disk_migrations.values():
        for operation in migration.operations:
            if isinstance(operation, MariaDBRunSQL):
                for statement in operation.sql:
                    names.update(re.findall(r"CREATE TRIGGER (\w+)", statement))
    return frozenset(names)


def _listed(names):
    shown = ", ".join(names[:_SHOWN])
    return shown + (f" and {len(names) - _SHOWN} more" if len(names) > _SHOWN else "")


def installed_guard_triggers(connection):
    """Installed trigger names, or None if the inventory view cannot be read."""
    try:
        with transaction.atomic(using=connection.alias), connection.cursor() as cursor:
            cursor.execute(f"SELECT trigger_name FROM {INVENTORY_VIEW}")
            return {name for (name,) in cursor.fetchall()}
    except DatabaseError:
        return None


def guard_findings(connection):
    """Why ClipCash's MariaDB guards are not all in place ([] = every guard installed)."""
    installed = installed_guard_triggers(connection)
    if installed is None:
        return [f"the guard inventory ({INVENTORY_VIEW}) cannot be read, so no guard can be confirmed: "
                "apply migrations as the migration login"]
    expected = expected_guard_triggers()
    findings = []
    missing = sorted(expected - installed)
    unexpected = sorted(installed - expected)
    if missing:
        findings.append(f"{len(missing)} of {len(expected)} guard triggers are missing ({_listed(missing)})")
    if unexpected:
        findings.append(f"{len(unexpected)} triggers not created by ClipCash's migrations exist "
                        f"({_listed(unexpected)})")
    with connection.cursor() as cursor:
        cursor.execute("SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS "
                       "WHERE CONSTRAINT_SCHEMA = DATABASE() AND CONSTRAINT_TYPE = 'CHECK'")
        present = {name for (name,) in cursor.fetchall()}
    absent = sorted(set(LOCK_CONSTRAINTS) - present)
    if absent:
        findings.append(f"lock constraints are missing ({_listed(absent)})")
    return findings
