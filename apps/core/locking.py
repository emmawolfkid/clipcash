"""Row and named locks that behave the same on PostgreSQL and MariaDB (DB-01)."""

import hashlib

from django.db import connection, connections
from django.db.transaction import TransactionManagementError

TRANSACTION_LOCK_TABLE = "core_transaction_lock"


def lock_own_rows(queryset, **kwargs):
    """``queryset.select_for_update(**kwargs)`` that locks only the queryset's own rows.

    PostgreSQL: FOR UPDATE OF the model's table, so rows joined in by select_related()
    are read but not locked. MariaDB has no FOR UPDATE OF and would lock every joined
    row as well (more locks, other lock orders), so there the join is dropped instead:
    related objects then load lazily, unlocked, exactly as with OF on PostgreSQL.
    """
    if connections[queryset.db].features.has_select_for_update_of:
        return queryset.select_for_update(of=("self",), **kwargs)
    return queryset.select_related(None).select_for_update(**kwargs)


def transaction_lock_mariadb(key):
    """MariaDB counterpart of ``pg_advisory_xact_lock``: serialize every transaction that takes
    the lock named ``key`` until it commits or rolls back.

    The key's row in core_transaction_lock (core migration 0004) is upserted, which takes its
    exclusive row lock at once (a duplicate-key INSERT would take a shared lock first, and two
    such waiters deadlock when both upgrade). The lock ends with the transaction, whatever the
    outcome; on rollback a newly inserted row disappears with it. GET_LOCK() is not used: it is
    held by the session and survives COMMIT and ROLLBACK. Rows stay after commit (one per key).
    """
    if not connection.in_atomic_block:
        raise TransactionManagementError("transaction_lock_mariadb() must run inside transaction.atomic().")
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    with connection.cursor() as cursor:
        cursor.execute(f"INSERT INTO {TRANSACTION_LOCK_TABLE} (lock_key) VALUES (%s) "
                       "ON DUPLICATE KEY UPDATE lock_key = lock_key", [digest])
