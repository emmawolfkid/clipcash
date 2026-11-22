"""DB-01 / MDEV-37474: the TRIGGER probe must tell "can see a trigger" from "holds TRIGGER".

MariaDB 10.11.16+ (11.4.10, 11.8.6, 12.2.2) lists a trigger in information_schema.TRIGGERS to any
login with INSERT, UPDATE or DELETE on its table, hiding only ACTION_STATEMENT and DEFINER. Before
that, a listed row meant TRIGGER. The scripted cursor below reproduces both server behaviours (the
local server may run either); the live tests check the real logins against the real server.
"""
from unittest import mock, skipUnless

from django.conf import settings
from django.db import DatabaseError, connection, connections
from django.test import SimpleTestCase, TestCase

from apps.core import mariadb_logins as logins
from apps.core.migration_ops import is_mariadb

GUARDS = [(f"guard_{index}", 0) for index in range(114)]  # Production: 114 guard triggers.
DENIED = DatabaseError(1227, "Access denied; you need (at least one of) the TRIGGER privilege(s)")


class ScriptedCursor:
    """Answers each statement from ``script`` ({statement prefix: rows or exception})."""

    def __init__(self, script):
        self.script, self.executed, self.rows = script, [], []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        self.executed.append(statement)
        for prefix, answer in self.script.items():
            if statement.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                self.rows = list(answer)
                return
        raise AssertionError(f"unexpected statement: {statement}")

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class ScriptedConnection:
    def __init__(self, script):
        self.cursor_ = ScriptedCursor(script)
        self.ops = mock.Mock(quote_name=lambda name: f"`{name}`")

    def cursor(self):
        return self.cursor_


def triggers(rows, show_create=DENIED):
    return {"SELECT TRIGGER_NAME": rows, "SHOW CREATE TRIGGER": show_create}


class TriggerProbeTests(SimpleTestCase):
    def probe(self, script):
        cursor = ScriptedCursor(script)
        return logins.probe_trigger_right(cursor), cursor.executed

    def test_restricted_login_on_new_servers_is_not_a_trigger_holder(self):
        # The production false positive: 114 rows visible, bodies hidden, SHOW CREATE TRIGGER denied.
        result, executed = self.probe(triggers(GUARDS))
        self.assertEqual(result, logins.ABSENT)
        self.assertEqual(executed[-1], "SHOW CREATE TRIGGER `guard_0`")

    def test_restricted_login_on_old_servers_sees_no_rows(self):
        result, executed = self.probe(triggers([]))
        self.assertEqual(result, logins.ABSENT)
        self.assertEqual(len(executed), 1)

    def test_database_level_trigger_privilege_is_found(self):
        self.assertEqual(self.probe(triggers([(name, 1) for name, _ in GUARDS]))[0], logins.HELD)

    def test_table_level_trigger_privilege_on_one_table_is_found(self):
        rows = GUARDS[:-1] + [("ledger_entry_finalize_check", 1)]
        self.assertEqual(self.probe(triggers(rows))[0], logins.HELD)

    def test_hidden_body_but_readable_trigger_counts_as_held(self):
        self.assertEqual(self.probe(triggers(GUARDS, show_create=[("guard_0", "", "CREATE TRIGGER ...")]))[0],
                         logins.HELD)

    def test_unexpected_answers_are_unverified_never_safe(self):
        for label, script in (
            ("catalog unreadable", {"SELECT TRIGGER_NAME": DatabaseError(1045, "denied")}),
            ("confirmation failed otherwise", triggers(GUARDS, show_create=DatabaseError(2013, "lost connection"))),
            ("trigger vanished", triggers(GUARDS, show_create=DatabaseError(1360, "Trigger does not exist"))),
            ("error without a code", triggers(GUARDS, show_create=DatabaseError())),
        ):
            with self.subTest(label):
                self.assertEqual(self.probe(script)[0], logins.UNVERIFIED)

    def test_trigger_names_are_quoted(self):
        _result, executed = self.probe(triggers([("odd`name", 0)]))
        self.assertEqual(executed[-1], "SHOW CREATE TRIGGER `odd``name`")


SAFE_CATALOG = ({"login clipcash_runtime@localhost": {"SELECT", "INSERT", "UPDATE", "DELETE", "USAGE"},
                 "PUBLIC": set()}, [])


class RuntimeFindingsTests(SimpleTestCase):
    """The probe's verdict reaches runtime_login_findings; catalog checks are unchanged."""

    def findings(self, script, catalog=SAFE_CATALOG, ddl=False):
        with mock.patch.object(logins, "effective_privileges", return_value=catalog), \
                mock.patch.object(logins, "probe_temporary_tables", return_value=[]):
            return logins.runtime_login_findings(ScriptedConnection(script), ddl_probes=ddl)

    def test_restricted_runtime_login_is_safe_on_both_server_behaviours(self):
        self.assertEqual(self.findings(triggers(GUARDS)), [])
        self.assertEqual(self.findings(triggers([])), [])

    def test_hidden_trigger_grant_is_reported_despite_a_clean_catalog(self):
        self.assertEqual(self.findings(triggers(GUARDS[:3] + [("guard_x", 1)])),
                         ["it holds TRIGGER on this database (found by probing; may come from a hidden grant)"])

    def test_unverified_trigger_right_is_reported_as_unsafe(self):
        found = self.findings({"SELECT TRIGGER_NAME": DatabaseError(1045, "denied")})
        self.assertEqual(found, ["whether it holds TRIGGER on this database could not be verified: treat as unsafe"])

    def test_catalog_privileges_are_still_reported(self):
        for label, catalog, expected in (
            ("global or database grant", ({"login x": {"SELECT", "TRIGGER"}, "PUBLIC": set()}, []),
             "login x grants TRIGGER on this database"),
            ("role, inherited or not", ({"login x": {"SELECT"}, "role r": {"TRIGGER", "DROP"}, "PUBLIC": set()},
                                        ["r"]), "role r grants DROP, TRIGGER on this database"),
            ("PUBLIC", ({"login x": {"SELECT"}, "PUBLIC": {"TRIGGER"}}, []), "PUBLIC grants TRIGGER on this database"),
            ("unreadable role", ({"login x": {"SELECT"}, "role r": None, "PUBLIC": set()}, ["r"]),
             "role r is granted to it but its privileges cannot be inspected: treat as unsafe"),
        ):
            with self.subTest(label):
                self.assertIn(expected, self.findings(triggers(GUARDS), catalog=catalog))

    def test_unreadable_grants_are_unsafe(self):
        with mock.patch.object(logins, "effective_privileges", side_effect=DatabaseError(1142, "denied")):
            found = logins.runtime_login_findings(ScriptedConnection({}), ddl_probes=False)
        self.assertEqual(found, ["its grants could not be inspected (DatabaseError): treat as unsafe"])

    def test_ddl_probes_still_detect_hidden_drop_and_alter(self):
        script = triggers(GUARDS) | {"DROP TABLE IF EXISTS": [],  # Ran: DROP held.
                                     "ALTER TABLE": DatabaseError(1146, "Table doesn't exist")}  # Not denied.
        self.assertEqual(self.findings(script, ddl=True),
                         [f"it holds {right} on this database (found by probing; may come from a hidden grant)"
                          for right in ("DROP", "ALTER")])
        denied = triggers(GUARDS) | {"DROP TABLE IF EXISTS": DatabaseError(1142, "denied"),
                                     "ALTER TABLE": DatabaseError(1142, "denied")}
        self.assertEqual(self.findings(denied, ddl=True), [])


@skipUnless(is_mariadb(connection) and "runtime" in settings.DATABASES, "MariaDB with the runtime login only.")
class LiveTriggerProbeTests(TestCase):
    databases = {"default", "runtime"}

    def test_runtime_login_does_not_hold_trigger(self):
        with connections["runtime"].cursor() as cursor:
            self.assertEqual(logins.probe_trigger_right(cursor), logins.ABSENT)
            # The confirmation step is refused to the runtime login whatever the server version.
            with self.assertRaises(DatabaseError) as refused:
                cursor.execute("SHOW CREATE TRIGGER ledger_entry_finalize_check")
            self.assertIn(refused.exception.args[0], logins._ACCESS_DENIED)

    def test_migration_login_legitimately_holds_trigger(self):
        if connection.settings_dict["USER"] == settings.DATABASES["runtime"]["USER"]:
            self.skipTest("The default alias runs as the runtime login here.")
        with connection.cursor() as cursor:
            self.assertEqual(logins.probe_trigger_right(cursor), logins.HELD)
