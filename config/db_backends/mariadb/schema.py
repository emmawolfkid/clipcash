"""
Uniqueness rules on MariaDB (DB-01).

MariaDB has no partial (WHERE ...) indexes, no functional indexes and no
NULLS NOT DISTINCT, and Django's MySQL backend silently skips every
UniqueConstraint that needs one of them. ClipCash relies on such constraints for
money and identity rules (one approval per membership, one open claim per Airtel
reference, one pending membership choice, case-insensitive email, ...), so here
they are created instead of skipped:

* each key part becomes an INVISIBLE STORED generated column holding the part's
  value when the constraint's condition holds and NULL otherwise;
* a UNIQUE index covers those columns. MariaDB unique indexes, like PostgreSQL's,
  never treat NULLs as equal, so rows outside the condition never conflict;
* nulls_distinct=False: a nullable part is stored as IFNULL(part, sentinel) next to
  a "part IS NULL" flag column, so NULLs collide with each other and with nothing else;
* text key columns use utf8mb4_nopad_bin, so keys compare exactly as PostgreSQL
  compares them (case-, accent- and trailing-space-sensitive). A functional
  Lower("email") key is therefore exactly PostgreSQL's lower(email) rule.

The generated columns are not model fields: Django never reads or writes them, and
INVISIBLE keeps them out of SELECT * as well. Columns named in a generated
expression cannot be renamed or retyped while the constraint exists; a later
migration must drop and re-add the constraint around such a change.

PostgreSQL ExclusionConstraints cannot be expressed in MariaDB. They are skipped
here, and the MariaDB guard triggers of the owning app enforce the same rule (see
the memberships subscription guard for sub_no_overlapping_periods).
"""

from django.contrib.postgres.constraints import ExclusionConstraint
from django.db import NotSupportedError
from django.db.backends.ddl_references import Statement, Table
from django.db.backends.mysql import schema
from django.db.backends.utils import truncate_name
from django.db.models import UniqueConstraint
from django.db.models.sql import Query

KEY_COLLATION = "utf8mb4_nopad_bin"
MAX_NAME = 64


def needs_emulation(condition=None, expressions=None, nulls_distinct=None):
    return bool(condition or expressions or nulls_distinct is False)


class DatabaseSchemaEditor(schema.DatabaseSchemaEditor):
    sql_create_emulated_unique = "ALTER TABLE %(table)s %(columns)s, ADD CONSTRAINT %(name)s UNIQUE (%(keys)s)"
    sql_delete_emulated_unique = "ALTER TABLE %(table)s DROP INDEX %(name)s, %(columns)s"

    def _unique_supported(self, condition=None, deferrable=None, include=None, expressions=None,
                          nulls_distinct=None):
        # Conditions, expressions and nulls_distinct are emulated; deferrable and
        # covering unique constraints stay unsupported (ClipCash uses neither).
        return super()._unique_supported(deferrable=deferrable, include=include)

    def _create_unique_sql(self, model, fields, name=None, condition=None, deferrable=None, include=None,
                           opclasses=None, expressions=None, nulls_distinct=None):
        if not needs_emulation(condition, expressions, nulls_distinct):
            return super()._create_unique_sql(model, fields, name=name, condition=condition,
                                              deferrable=deferrable, include=include, opclasses=opclasses,
                                              expressions=expressions, nulls_distinct=nulls_distinct)
        if name is None or deferrable or include or opclasses:
            raise NotSupportedError(f"MariaDB cannot emulate unique constraint {name!r} on {model.__name__}.")
        columns = self._emulated_unique_columns(model, fields, name, condition, expressions, nulls_distinct)
        return Statement(
            self.sql_create_emulated_unique,
            table=Table(model._meta.db_table, self.quote_name),
            columns=", ".join(f"ADD COLUMN {self.quote_name(column)} {definition}"
                              for column, definition in columns),
            name=self.quote_name(name),
            keys=", ".join(self.quote_name(column) for column, _definition in columns),
        )

    def add_constraint(self, model, constraint):
        if isinstance(constraint, ExclusionConstraint):
            return  # Enforced by the owning app's MariaDB guard trigger instead.
        super().add_constraint(model, constraint)

    def remove_constraint(self, model, constraint):
        if isinstance(constraint, ExclusionConstraint):
            return
        if isinstance(constraint, UniqueConstraint) and needs_emulation(
            constraint.condition, constraint.expressions, constraint.nulls_distinct
        ):
            columns = self._emulated_unique_columns(
                model,
                [model._meta.get_field(field_name) for field_name in constraint.fields],
                constraint.name,
                constraint._get_condition_sql(model, self),
                constraint._get_index_expressions(model, self),
                constraint.nulls_distinct,
            )
            self.execute(Statement(
                self.sql_delete_emulated_unique,
                table=Table(model._meta.db_table, self.quote_name),
                name=self.quote_name(constraint.name),
                columns=", ".join(f"DROP COLUMN {self.quote_name(column)}" for column, _definition in columns),
            ))
            return
        super().remove_constraint(model, constraint)

    # --- Generated key columns -------------------------------------------------------------

    def _emulated_unique_columns(self, model, fields, name, condition, expressions, nulls_distinct):
        """[(column name, column definition)] of the generated key columns, in index order."""
        parts = [(self.quote_name(field.column), field, field.null) for field in fields]
        if expressions:
            compiler = Query(model, alias_cols=False).get_compiler(connection=self.connection)
            for expression in expressions.get_source_expressions():
                sql, params = compiler.compile(expression)
                sql = sql % tuple(self.quote_value(param) for param in params)
                parts.append((sql, expression.output_field, True))

        columns = []
        for sql, output_field, nullable in parts:
            db_type = (output_field.db_type(self.connection) or "").lower()
            is_text = db_type.startswith(("varchar", "char"))
            # Native UUID (Django on MariaDB 10.7+) and integers compare exactly already.
            sentinel = {"uuid": "'00000000-0000-0000-0000-000000000000'"}.get(db_type, "0")
            if not (is_text or db_type == "uuid" or db_type.startswith(("bigint", "int", "smallint"))):
                raise NotSupportedError(f"Unique constraint {name!r}: cannot index a key of type {db_type!r}.")
            column_type = f"{db_type} CHARACTER SET utf8mb4 COLLATE {KEY_COLLATION}" if is_text else db_type
            key = sql
            flag = None
            if nulls_distinct is False and nullable:
                key = f"IFNULL({sql}, {sentinel if not is_text else repr('')})"
                flag = f"({sql}) IS NULL"
            columns.append((self._key_column_name(name, len(columns)), self._generated(column_type, key, condition)))
            if flag is not None:
                columns.append((self._key_column_name(name, len(columns)), self._generated("tinyint(1)", flag,
                                                                                           condition)))
        return columns

    @staticmethod
    def _generated(column_type, value, condition):
        if condition:
            value = f"IF({condition}, {value}, NULL)"
        return f"{column_type} AS ({value}) STORED INVISIBLE"

    @staticmethod
    def _key_column_name(constraint_name, position):
        return truncate_name(f"{constraint_name}__k{position}", MAX_NAME)
