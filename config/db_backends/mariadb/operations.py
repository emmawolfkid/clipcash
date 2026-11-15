from django.db.backends.mysql import operations


class DatabaseOperations(operations.DatabaseOperations):
    def sql_flush(self, style, tables, *, reset_sequences=False, allow_cascade=False):
        # Always TRUNCATE, as PostgreSQL's flush does: DELETE would fire ClipCash's
        # append-only guard triggers (flush is used by tests and by `manage.py flush`).
        # ClipCash's primary keys are UUIDs, so the AUTO_INCREMENT reset only touches
        # Django's own tables.
        return super().sql_flush(style, tables, reset_sequences=True, allow_cascade=allow_cascade)
