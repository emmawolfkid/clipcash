"""
Vendor-specific raw SQL for migrations (DB-01: PostgreSQL and MariaDB builds).

ClipCash's database guards are triggers written separately for each database:
PL/pgSQL for PostgreSQL and SQL/PSM for MariaDB. A migration lists both
operations side by side; each runs only on its own database and is a no-op on
the other, so the migration graph is the same for both builds.

These classes are imported by historical migrations: never change their behaviour.

MariaDB guard conventions (the PostgreSQL ERRCODE each one replaces):
* check_violation       -> SIGNAL SQLSTATE '23000' SET MYSQL_ERRNO = 4025, MESSAGE_TEXT = ...
                           (Django raises IntegrityError, as on PostgreSQL);
* insufficient_privilege -> SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = ...
                           (Django raises OperationalError; PostgreSQL gives ProgrammingError;
                           both are DatabaseError, which is what ClipCash catches).
* Changes are detected with NOT (NEW.x <=> OLD.x) on BINARY values, so a guard sees
  every byte change, including case-only and trailing-space changes that
  MariaDB's case-insensitive collations would otherwise call equal.
* MariaDB statements are given as a list: each element is one complete statement
  (a CREATE TRIGGER body contains semicolons and must not be split).
"""

from django.db import migrations


def is_mariadb(connection):
    return connection.vendor == "mysql" and connection.mysql_is_mariadb


class _VendorRunSQL(migrations.RunSQL):
    def applies_to(self, connection):
        raise NotImplementedError

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if self.applies_to(schema_editor.connection):
            super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if self.applies_to(schema_editor.connection):
            super().database_backwards(app_label, schema_editor, from_state, to_state)


class PostgreSQLRunSQL(_VendorRunSQL):
    """RunSQL on PostgreSQL only."""

    def applies_to(self, connection):
        return connection.vendor == "postgresql"

    def describe(self):
        return "Raw SQL operation (PostgreSQL only)"


class MariaDBRunSQL(_VendorRunSQL):
    """RunSQL on MariaDB only."""

    def applies_to(self, connection):
        return is_mariadb(connection)

    def describe(self):
        return "Raw SQL operation (MariaDB only)"


# --- MariaDB trigger building blocks (frozen: historical migrations use them) -----------------


def _literal(text):
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"


def check_error(message):
    """Refusal equivalent to PostgreSQL's check_violation (IntegrityError in Django)."""
    return f"SIGNAL SQLSTATE '23000' SET MYSQL_ERRNO = 4025, MESSAGE_TEXT = {_literal(message)};"


def privilege_error(message):
    """Refusal equivalent to PostgreSQL's insufficient_privilege (a DatabaseError in Django)."""
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = {_literal(message)};"


def changed(*columns):
    """SQL true when any column's stored bytes differ between OLD and NEW (NULL-safe)."""
    return "(" + " OR ".join(f"NOT (BINARY NEW.{column} <=> BINARY OLD.{column})" for column in columns) + ")"


def trigger(name, timing, table, body):
    """One complete CREATE TRIGGER statement (MariaDB: one event per trigger)."""
    return f"CREATE TRIGGER {name} {timing} ON {table} FOR EACH ROW\nBEGIN\n{body}\nEND"


def drop_triggers(*names):
    return [f"DROP TRIGGER IF EXISTS {name}" for name in names]
