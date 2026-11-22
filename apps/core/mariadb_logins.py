"""
DB-01: MariaDB login separation checks.

ClipCash's database guards are triggers. They run with the rights of their DEFINER (the
migration login, which owns the schema), so the login the application runs as needs only
SELECT, INSERT, UPDATE and DELETE. Anything more defeats the guards:

* CREATE TEMPORARY TABLES: a session's temporary table shadows the table a trigger reads
  (RG-03 on MariaDB; PostgreSQL closes it by pinning search_path), so a guard can be fed
  fake data, e.g. a larger campaign budget or a fake ledger;
* TRIGGER, DROP, ALTER, CREATE...: the guards themselves can be removed or replaced;
* GRANT OPTION, SUPER and other administrative rights.

A privilege counts wherever it comes from: the login's own grants (global, wildcard
database patterns, database, table or column level), any role granted to it, directly or
through other roles, default or not (a session can SET ROLE itself), PUBLIC, and grants
the catalog does not show at all: an anonymous ''@'%' row in mysql.db (classic installs,
XAMPP included, grant ALL on `test` and `test\\_%` that way) reaches every login and appears
in neither SHOW GRANTS nor information_schema. So the catalog is checked first, then the
rights are probed by behaviour: CREATE TEMPORARY TABLE for real (with no role, the default
role and every role), and TRIGGER, DROP and ALTER without changing anything.

Run `manage.py check_mariadb_logins` against each environment (the migration login as
"default", the runtime login as "runtime"); it never changes data.
"""

import re

from django.db import DatabaseError

RUNTIME_ALLOWED = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "USAGE"})
# What `migrate` needs to create ClipCash's schema and guard triggers.
MIGRATION_REQUIRED = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "INDEX",
                                "REFERENCES", "TRIGGER", "CREATE VIEW"})
ALL_PRIVILEGES = "ALL PRIVILEGES"

_GRANT = re.compile(r"^GRANT\s+(?P<privileges>.+?)\s+ON\s+(?P<object>.+?)\s+TO\s+", re.IGNORECASE | re.DOTALL)
_ROLE_GRANT = re.compile(r"^GRANT\s+(?P<roles>`[^`]*`(?:\s*,\s*`[^`]*`)*)\s+TO\s+", re.IGNORECASE)
_COLUMNS = re.compile(r"\([^)]*\)")
_IDENTIFIER = r"(`(?:[^`]|``)*`|\*|[\w$]+)"
_OBJECT = re.compile(rf"^(?:(?:PROCEDURE|FUNCTION|PACKAGE(?: BODY)?)\s+)?{_IDENTIFIER}\.{_IDENTIFIER}$", re.IGNORECASE)


def _unquote(identifier):
    if identifier.startswith("`"):
        return identifier[1:-1].replace("``", "`")
    return identifier


def _database_pattern_matches(pattern, database):
    """MariaDB database-level grants may use LIKE wildcards (_ and %, \\ escapes)."""
    regex, escaped = "", False
    for char in pattern:
        if escaped:
            regex += re.escape(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "%":
            regex += ".*"
        elif char == "_":
            regex += "."
        else:
            regex += re.escape(char)
    return re.fullmatch(regex, database, re.IGNORECASE) is not None


def parse_grant(line, database):
    """Privileges (upper case) that a SHOW GRANTS line gives on ``database``; an empty set
    when the line is about another database or is not a privilege grant."""
    match = _GRANT.match(line.strip())
    if not match:
        return set()
    target = _OBJECT.match(match.group("object").strip())
    if target is not None:
        db = _unquote(target.group(1))
        if db != "*" and not _database_pattern_matches(db, database):
            return set()
    # An object we cannot parse is treated as applying (fail closed).
    privileges = {part.strip().upper() for part in _COLUMNS.sub("", match.group("privileges")).split(",")}
    privileges.discard("")
    if re.search(r"\bWITH\b.*\bGRANT\s+OPTION\b", line, re.IGNORECASE):
        privileges.add("GRANT OPTION")
    if "ALL" in privileges:
        privileges = (privileges - {"ALL"}) | {ALL_PRIVILEGES}
    return privileges


def parse_role_grants(line):
    """Roles that a SHOW GRANTS line grants (GRANT `role` TO ...)."""
    match = _ROLE_GRANT.match(line.strip())
    if not match:
        return []
    return [_unquote(role.strip()) for role in match.group("roles").split(",")]


def _show_grants(cursor, subject):
    cursor.execute(f"SHOW GRANTS FOR {subject}")
    return [row[0] for row in cursor.fetchall()]


def _quote_role(role):
    return "`" + role.replace("`", "``") + "`"


def effective_privileges(connection):
    """{source: privileges on this database} for the connected login, its roles (recursively,
    from its grants and information_schema.APPLICABLE_ROLES) and PUBLIC. Raises DatabaseError
    if a source cannot be read."""
    database = connection.settings_dict["NAME"]
    sources, roles, seen = {}, [], set()
    with connection.cursor() as cursor:
        cursor.execute("SELECT CURRENT_USER()")
        (login,) = cursor.fetchone()
        own = _show_grants(cursor, "CURRENT_USER()")
        sources[f"login {login}"] = set().union(*(parse_grant(line, database) for line in own))
        roles.extend(role for line in own for role in parse_role_grants(line))
        cursor.execute("SELECT ROLE_NAME FROM information_schema.APPLICABLE_ROLES")
        roles.extend(role for (role,) in cursor.fetchall())
        while roles:
            role = roles.pop()
            if role in seen or role.upper() == "PUBLIC":
                continue
            seen.add(role)
            try:
                lines = _show_grants(cursor, _quote_role(role))
            except DatabaseError:
                sources[f"role {role}"] = None  # Not readable by this login (e.g. inherited only).
                continue
            sources[f"role {role}"] = set().union(*(parse_grant(line, database) for line in lines))
            roles.extend(nested for line in lines for nested in parse_role_grants(line))
        sources["PUBLIC"] = set().union(*(parse_grant(line, database) for line in _show_grants(cursor, "PUBLIC")))
    return sources, sorted(seen)


def probe_temporary_tables(connection, roles):
    """Try CREATE TEMPORARY TABLE with no role, the default role and every role; returns the
    contexts in which it worked (the probe table is dropped at once). Restores no role."""
    allowed = []
    with connection.cursor() as cursor:
        contexts = [("no role", "SET ROLE NONE")] + [(f"role {role}", f"SET ROLE {_quote_role(role)}") for role in roles]
        cursor.execute("SELECT CURRENT_ROLE()")
        (default_role,) = cursor.fetchone()
        if default_role:
            contexts.insert(0, (f"default role {default_role}", f"SET ROLE {_quote_role(default_role)}"))
        for label, activate in contexts:
            try:
                cursor.execute(activate)
            except DatabaseError:
                continue  # Only directly granted roles can be activated; nested ones come with them.
            try:
                cursor.execute("CREATE TEMPORARY TABLE clipcash_login_probe (probe INT)")
            except DatabaseError:
                continue
            cursor.execute("DROP TEMPORARY TABLE IF EXISTS clipcash_login_probe")
            allowed.append(label)
        cursor.execute(f"SET ROLE {_quote_role(default_role)}" if default_role else "SET ROLE NONE")
    return allowed


PROBE_TABLE = "clipcash_privilege_probe_never_created"
_ACCESS_DENIED = {1044, 1142, 1227}  # Database, table and command level "access denied".

HELD, ABSENT, UNVERIFIED = "held", "absent", "unverified"
# One row per trigger in this database that the login can see, and whether its body or definer is
# shown (1) or hidden (0).
_VISIBLE_TRIGGERS = ("SELECT TRIGGER_NAME, (ACTION_STATEMENT IS NOT NULL OR DEFINER IS NOT NULL)"
                     " FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()")


def probe_trigger_right(cursor):
    """Whether the login holds TRIGGER on any table of this database: HELD, ABSENT or UNVERIFIED.

    Which rows information_schema.TRIGGERS shows depends on the server version:

    * before MariaDB 10.11.16 (and 11.4.10, 11.8.6, 12.2.2) a row is listed only when the login
      holds TRIGGER on its table, with its body and definer;
    * from those versions on (MDEV-37474), a row is also listed when the login holds only INSERT,
      UPDATE or DELETE on its table, but ACTION_STATEMENT and DEFINER are then NULL. Only TRIGGER
      reveals them.

    So a row whose body or definer is shown proves TRIGGER on that table, and merely seeing a row
    proves nothing. When rows are listed with the body hidden, SHOW CREATE TRIGGER (which needs
    TRIGGER and changes nothing) confirms by behaviour. Every unexpected outcome is UNVERIFIED,
    which callers must treat as unsafe."""
    try:
        cursor.execute(_VISIBLE_TRIGGERS)
        rows = cursor.fetchall()
    except DatabaseError:
        return UNVERIFIED
    if not rows:
        return ABSENT
    if any(revealed for _name, revealed in rows):
        return HELD
    try:
        cursor.execute(f"SHOW CREATE TRIGGER `{rows[0][0].replace('`', '``')}`")
        cursor.fetchall()
    except DatabaseError as error:
        code = error.args[0] if error.args else None
        return ABSENT if code in _ACCESS_DENIED else UNVERIFIED
    return HELD  # The body was hidden from information_schema yet readable: TRIGGER after all.


def probe_schema_rights(connection, *, ddl=True):
    """Rights that grants can hide (e.g. an anonymous ''@'%' row on `test\\_%` databases, which
    neither SHOW GRANTS nor information_schema.SCHEMA_PRIVILEGES shows), found by behaviour and
    without changing anything: TRIGGER (see probe_trigger_right), DROP (DROP TABLE IF EXISTS on
    a table that never exists) and ALTER (denied vs "no such table" on that same name).

    Returns (held, unverified): rights the login holds, and rights that could not be verified."""
    held, unverified = [], []
    table = connection.ops.quote_name(PROBE_TABLE)
    with connection.cursor() as cursor:
        trigger = probe_trigger_right(cursor)
        if trigger == HELD:
            held.append("TRIGGER")
        elif trigger != ABSENT:
            unverified.append("TRIGGER")
        if not ddl:
            return held, unverified
        for right, statement in (("DROP", f"DROP TABLE IF EXISTS {table}"),
                                 ("ALTER", f"ALTER TABLE {table} ADD COLUMN probe INT")):
            try:
                cursor.execute(statement)
            except DatabaseError as error:
                code = error.args[0] if error.args else None
                if code not in _ACCESS_DENIED:
                    held.append(right)  # Allowed to try: it only failed because the table is absent.
                continue
            held.append(right)
    return held, unverified


def runtime_login_findings(connection, *, ddl_probes=True):
    """Problems that make ``connection``'s login unsafe to run ClipCash as (empty when safe).

    ``ddl_probes=False`` (health page, demo_readiness, tests): skip the DROP and ALTER probes.
    They are DDL, and when the login does hold those rights MariaDB would implicitly commit the
    caller's open transaction. The catalog, the temporary-table probe (no implicit commit) and
    the TRIGGER probe still run; check_mariadb_logins runs every probe."""
    findings = []
    try:
        sources, roles = effective_privileges(connection)
    except DatabaseError as error:
        return [f"its grants could not be inspected ({type(error).__name__}): treat as unsafe"]
    for source, privileges in sources.items():
        if privileges is None:
            findings.append(f"{source} is granted to it but its privileges cannot be inspected: treat as unsafe")
            continue
        extra = sorted(privileges - RUNTIME_ALLOWED)
        if extra:
            findings.append(f"{source} grants {', '.join(extra)} on this database")
    for context in probe_temporary_tables(connection, roles):
        findings.append(f"it can CREATE TEMPORARY TABLE ({context}): guard triggers can be shadowed")
    held, unverified = probe_schema_rights(connection, ddl=ddl_probes)
    for right in held:
        findings.append(f"it holds {right} on this database (found by probing; may come from a hidden grant)")
    for right in unverified:
        findings.append(f"whether it holds {right} on this database could not be verified: treat as unsafe")
    return findings


def global_privileges(lines):
    """Privileges (upper case) that SHOW GRANTS lines give ON *.* (server-wide)."""
    found = set()
    for line in lines:
        match = _GRANT.match(line.strip())
        if not match or match.group("object").strip() != "*.*":
            continue
        privileges = {part.strip().upper() for part in _COLUMNS.sub("", match.group("privileges")).split(",")}
        if "ALL" in privileges or ALL_PRIVILEGES in privileges:
            privileges |= {ALL_PRIVILEGES}
        found |= privileges
    return found


def has_global_super(connection):
    """True when the connected login itself holds SUPER (or ALL PRIVILEGES) ON *.*."""
    with connection.cursor() as cursor:
        held = global_privileges(_show_grants(cursor, "CURRENT_USER()"))
    return bool(held & {"SUPER", ALL_PRIVILEGES})


def migration_login_findings(connection, runtime_connection=None):
    """Problems that stop ``connection``'s login from migrating ClipCash, or that mean it is
    not a separate login from the runtime one."""
    findings = []
    try:
        sources, _roles = effective_privileges(connection)
    except DatabaseError as error:
        return [f"its grants could not be inspected ({type(error).__name__})"]
    privileges = set().union(*(found for found in sources.values() if found))
    if ALL_PRIVILEGES not in privileges:
        missing = sorted(MIGRATION_REQUIRED - privileges)
        if missing:
            findings.append(f"it lacks {', '.join(missing)} on this database (needed by migrate)")
    with connection.cursor() as cursor:
        cursor.execute("SELECT @@log_bin, @@log_bin_trust_function_creators")
        log_bin, trust = cursor.fetchone()
    # PRD-00B: only a GLOBAL SUPER (or global ALL) lets a login create triggers while binary logging
    # is on. cPanel grants "ALL PRIVILEGES" on the database only, which does not include SUPER, so
    # the database-level privileges above must not be taken as enough.
    if log_bin and not trust and not has_global_super(connection):
        findings.append("binary logging is on without log_bin_trust_function_creators and the login has no "
                        "global SUPER: it cannot create triggers (ask the host to set "
                        "log_bin_trust_function_creators=1)")
    if runtime_connection is not None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT CURRENT_USER()")
            (migration,) = cursor.fetchone()
        with runtime_connection.cursor() as cursor:
            cursor.execute("SELECT CURRENT_USER()")
            (runtime,) = cursor.fetchone()
        if migration.rsplit("@", 1)[0] == runtime.rsplit("@", 1)[0]:
            findings.append("the migration and runtime logins are the same account")
    return findings
