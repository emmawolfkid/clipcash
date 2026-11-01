"""
Atomic, multi-process rate-limit counters in PostgreSQL.

Used by apps.accounts.throttling when ACCOUNTS_THROTTLE_BACKEND = "database"
(the production default). Each hit is one INSERT ... ON CONFLICT DO UPDATE,
so concurrent workers can never lose an increment. Keys are SHA-256 digests;
no email address or IP is stored. Errors propagate to the caller, which fails
closed.
"""

from django.db import connection, transaction

TABLE = "core_ratelimitcounter"


def hit(key, window_seconds):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO {TABLE} (key, count, expires_at)
            VALUES (%s, 1, now() + make_interval(secs => %s))
            ON CONFLICT (key) DO UPDATE SET
                count = CASE WHEN {TABLE}.expires_at <= now() THEN 1 ELSE {TABLE}.count + 1 END,
                expires_at = CASE WHEN {TABLE}.expires_at <= now()
                                  THEN now() + make_interval(secs => %s) ELSE {TABLE}.expires_at END
            RETURNING count
            """,
            [key, window_seconds, window_seconds],
        )
        return cursor.fetchone()[0]


def count(key):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(f"SELECT count FROM {TABLE} WHERE key = %s AND expires_at > now()", [key])
        row = cursor.fetchone()
        return row[0] if row else 0


def reset(key):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {TABLE} WHERE key = %s", [key])
