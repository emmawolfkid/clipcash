"""
ClipCash's MariaDB backend (DB-01): Django's MySQL backend, MariaDB 10.11 or later only.

It differs from the stock backend only in its schema editor (see schema.py), which
creates the uniqueness rules Django would otherwise silently skip on MariaDB, and in
flushing with TRUNCATE (operations.py), as PostgreSQL does. Its
``vendor`` stays "mysql", so everything Django and ClipCash do for MySQL/MariaDB
applies unchanged.
"""

from django.core.exceptions import ImproperlyConfigured
from django.db.backends.mysql import base

from .operations import DatabaseOperations
from .schema import DatabaseSchemaEditor

MINIMUM_VERSION = (10, 11)


class DatabaseWrapper(base.DatabaseWrapper):
    SchemaEditorClass = DatabaseSchemaEditor
    ops_class = DatabaseOperations

    def check_database_version_supported(self):
        super().check_database_version_supported()
        if not self.mysql_is_mariadb:
            raise ImproperlyConfigured("ClipCash's MariaDB backend refuses MySQL servers; use MariaDB 10.11+.")
        if self.mysql_version < MINIMUM_VERSION:
            found = ".".join(map(str, self.mysql_version))
            raise ImproperlyConfigured(f"ClipCash needs MariaDB 10.11 or later (found {found}).")
