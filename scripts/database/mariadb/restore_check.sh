#!/usr/bin/env bash
# PRD-00B: restore drill. Restores a backup.sh dump into an EMPTY scratch database and verifies it.
#
#   CLIPCASH_DB_OPTION_FILE=<path to restore.cnf> CLIPCASH_RESTORE_DB=<scratch database> \
#       bash scripts/database/mariadb/restore_check.sh <clipcash-....sql.gz>
#
# Safety (it can never overwrite the application database):
# * the target name must contain "restore" or start with "test_" (cPanel: <account>_clipcash_restore);
# * the target must exist, use utf8mb4 and have NO tables or views;
# * CLIPCASH_PRODUCTION_DB, if set, must differ from the target;
# * the dump has no DROP statements, so even a wrong target would fail rather than replace tables.
#
# The option file ([client] user/password/host) must be a login that may create tables, triggers
# and views in the scratch database (the migration login of that environment). Triggers and the
# guard-inventory view are re-created with THAT login as their DEFINER: the dump's original DEFINER
# clauses are removed while restoring (only on trigger/view definition lines), because the source
# login usually does not exist on the restore server.
#
# Data is loaded before each table's triggers exist (mysqldump writes them after the table's rows),
# so the guards never reject historical rows; afterwards every guard is back and enforcing.
#
# Checks: checksum, completeness, table/trigger counts against the manifest, row counts per table,
# ledger balances = sum of posted lines, every journal entry balanced, the locked money tables empty.
# PRD-04: if the backup has a payment-evidence archive (<dump>.evidence.tar.gz), it is verified and
# every receipt recorded in the restored database must be in it. Set CLIPCASH_RESTORE_EVIDENCE_DIR
# to an EMPTY (or new) directory to also extract the files there for a staging copy.
# Exit status 1 on any mismatch. Never prints credentials. Afterwards run the application checks
# printed at the end against the scratch copy (never production).
set -euo pipefail

file="${1:-}"
option_file="${CLIPCASH_DB_OPTION_FILE:-}"
target="${CLIPCASH_RESTORE_DB:-}"
evidence_target="${CLIPCASH_RESTORE_EVIDENCE_DIR:-}"
if [ -z "$file" ] || [ -z "$option_file" ] || [ -z "$target" ]; then
    echo "usage: CLIPCASH_DB_OPTION_FILE=... CLIPCASH_RESTORE_DB=... bash scripts/database/mariadb/restore_check.sh <dump.sql.gz>" >&2
    exit 2
fi
case "$target" in
    *[!A-Za-z0-9_]*) echo "CLIPCASH_RESTORE_DB may contain only letters, digits and underscores." >&2; exit 2 ;;
esac
case "$target" in
    *restore*|test_*) ;;
    *) echo "Refusing: '$target' does not contain 'restore' and does not start with 'test_'." >&2; exit 2 ;;
esac
if [ -n "${CLIPCASH_PRODUCTION_DB:-}" ] && [ "$target" = "$CLIPCASH_PRODUCTION_DB" ]; then
    echo "Refusing: '$target' is the production database." >&2
    exit 2
fi
[ -f "$file" ] || { echo "Dump not found: $file" >&2; exit 2; }
[ -f "$option_file" ] || { echo "Option file not found: $option_file" >&2; exit 2; }
mode="$(stat -c '%a' "$option_file" 2>/dev/null || echo unknown)"
if [ "${CLIPCASH_ALLOW_OPEN_OPTION_FILE:-0}" != "1" ] && [ "$mode" != "600" ] && [ "$mode" != "400" ]; then
    echo "Refusing: the option file must be private (chmod 600), not mode $mode." >&2
    exit 2
fi

pick() { for tool in "$@"; do command -v "$tool" >/dev/null 2>&1 && { echo "$tool"; return; }; done; echo "$1"; }
CLIENT="${MARIADB_CLIENT:-$(pick mariadb mysql)}"
# Output without carriage returns (Windows clients print CRLF).
sql() { "$CLIENT" --defaults-extra-file="$option_file" --batch --skip-column-names "$target" -e "$1" | tr -d '\r'; }

dir="$(cd "$(dirname "$file")" && pwd)"
base="$(basename "$file" .sql.gz)"
manifest="$dir/$base.manifest"
if [ -f "$dir/$base.sha256" ]; then
    (cd "$dir" && sha256sum --check --quiet "$base.sha256")
    echo "checksum: OK"
elif [ "${CLIPCASH_ALLOW_UNVERIFIED_DUMP:-0}" = "1" ]; then
    echo "checksum: no .sha256 file next to the dump (NOT verified; allowed by CLIPCASH_ALLOW_UNVERIFIED_DUMP)"
else
    echo "Refusing: no $base.sha256 next to the dump, so it cannot be verified." >&2
    exit 2
fi
gzip -t "$file"
gzip -dc "$file" | tail -n 5 | grep -q "^-- Dump completed" || { echo "Dump is incomplete." >&2; exit 1; }
evidence="$dir/$base.evidence.tar.gz"
if [ -n "$evidence_target" ]; then
    [ -f "$evidence" ] || { echo "Refusing: CLIPCASH_RESTORE_EVIDENCE_DIR is set but the backup has no evidence archive." >&2; exit 2; }
    if [ -d "$evidence_target" ] && [ -n "$(ls -A "$evidence_target")" ]; then
        echo "Refusing: $evidence_target is not empty." >&2
        exit 2
    fi
fi
evidence_listing=""
if [ -f "$evidence" ]; then
    evidence_listing="$(tar -tzf - < "$evidence" | sed 's#^\./##' | grep -v '/$' | grep -v '^\.$' || true)"
    tar -tzf - < "$evidence" > /dev/null  # set -e: an unreadable archive stops here.
fi

charset="$(sql "SELECT DEFAULT_CHARACTER_SET_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = DATABASE()")"
[ "$charset" = "utf8mb4" ] || { echo "Refusing: '$target' uses $charset; create it as utf8mb4 / utf8mb4_unicode_ci." >&2; exit 2; }
existing="$(sql "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()")"
[ "$existing" = "0" ] || { echo "Refusing: '$target' is not empty ($existing tables or views)." >&2; exit 2; }

echo "Restoring into '$target' ..."
gzip -dc "$file" \
    | sed -E '/^\/\*!50[0-9]{3} /s/DEFINER=`[^`]*`@`[^`]*`//' \
    | "$CLIENT" --defaults-extra-file="$option_file" --default-character-set=utf8mb4 "$target"

failures=0
report() { # label, actual, expected
    if [ "$2" = "$3" ]; then echo "OK   $1: $2"; else echo "FAIL $1: $2 (expected $3)"; failures=$((failures + 1)); fi
}
expected() { [ -f "$manifest" ] && sed -n "s/^$1=//p" "$manifest" | head -n 1; }

tables="$(sql "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'")"
triggers="$(sql "SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()")"
views="$(sql "SELECT COUNT(*) FROM information_schema.VIEWS WHERE TABLE_SCHEMA = DATABASE()")"
if [ -f "$manifest" ]; then
    report "tables" "$tables" "$(expected tables_in_dump)"
    report "triggers" "$triggers" "$(expected triggers_on_server)"
    mismatched=0
    while IFS='=' read -r key value; do
        case "$key" in rows.*) ;; *) continue ;; esac
        table="${key#rows.}"
        actual="$(sql "SELECT COUNT(*) FROM \`$table\`" 2>/dev/null || echo missing)"
        if [ "$actual" != "$value" ]; then
            echo "DIFF rows in $table: $actual restored, $value in manifest"
            mismatched=$((mismatched + 1))
        fi
    done < "$manifest"
    if [ "$mismatched" -eq 0 ]; then echo "OK   row counts: every table matches the manifest"; else
        echo "FAIL row counts: $mismatched tables differ (expected only if the site was written to during the backup)"
        failures=$((failures + 1)); fi
else
    echo "INFO no manifest: tables $tables, triggers $triggers (compare with the source by hand)"
fi
report "guard-inventory view present" "$views" "1"
report "ledger accounts whose balance differs from their posted lines" "$(sql "
    SELECT COUNT(*) FROM wallets_ledgeraccount a WHERE a.debit_balance <> COALESCE((
        SELECT SUM(CASE l.side WHEN 'DEBIT' THEN l.amount ELSE -l.amount END)
        FROM wallets_journalline l JOIN wallets_journalentry e ON e.id = l.entry_id
        WHERE l.account_id = a.id AND e.status = 'POSTED'), 0)")" "0"
report "posted journal entries that do not balance" "$(sql "
    SELECT COUNT(*) FROM (SELECT e.id FROM wallets_journalentry e JOIN wallets_journalline l ON l.entry_id = e.id
        WHERE e.status = 'POSTED' GROUP BY e.id
        HAVING SUM(CASE l.side WHEN 'DEBIT' THEN l.amount ELSE 0 END)
            <> SUM(CASE l.side WHEN 'CREDIT' THEN l.amount ELSE 0 END)) unbalanced")" "0"
report "real withdrawal requests (locked)" "$(sql "SELECT COUNT(*) FROM withdrawals_withdrawalrequest")" "0"
report "member wallet transactions (locked)" "$(sql "SELECT COUNT(*) FROM wallets_wallettransaction")" "0"
# PRD-04: payment receipts are files; every one the restored database records must be in the archive.
has_evidence_table="$(sql "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'payments_manualpaymentevidence'")"
recorded=""
[ "$has_evidence_table" = "1" ] && recorded="$(sql "SELECT storage_name FROM payments_manualpaymentevidence ORDER BY storage_name")"
recorded_count="$(printf '%s' "$recorded" | grep -c . || true)"
if [ -f "$evidence" ]; then
    missing="$(comm -23 <(printf '%s\n' "$recorded" | grep . | sort) <(printf '%s\n' "$evidence_listing" | grep . | sort) | grep -c . || true)"
    report "payment receipts recorded in the database but missing from the evidence archive" "$missing" "0"
    echo "INFO evidence archive: $(printf '%s\n' "$evidence_listing" | grep -c . || true) files; $recorded_count recorded in the database"
    if [ -n "$evidence_target" ]; then
        mkdir -p "$evidence_target"
        chmod 700 "$evidence_target" 2>/dev/null || true
        tar -C "$evidence_target" -xzf - < "$evidence"
        echo "INFO evidence extracted to $evidence_target (point DJANGO_PAYMENTS_EVIDENCE_ROOT of the staging copy there)"
    fi
elif [ "$recorded_count" != "0" ]; then
    report "payment receipts recorded but the backup has no evidence archive" "$recorded_count" "0"
fi
echo "INFO migrations recorded: $(sql "SELECT COUNT(*) FROM django_migrations")"
echo "INFO accounts: $(sql "SELECT COUNT(*) FROM accounts_user"), audit events: $(sql "SELECT COUNT(*) FROM audit_auditevent"), journal entries: $(sql "SELECT COUNT(*) FROM wallets_journalentry")"

if [ "$failures" -ne 0 ]; then
    echo "Restore into '$target' FAILED $failures check(s). Do not rely on this backup." >&2
    exit 1
fi
cat <<EOF
Restore into '$target' passed the database checks. Now check the application against the copy
(never production), with the scratch database as DB_NAME and the migration login as DB_USER:
  python manage.py migrate --check          # no unapplied migrations
  python manage.py check_mariadb_logins     # with DB_RUNTIME_USER set: guards installed, logins separated
Then drop '$target' (cPanel > MySQL Databases) unless you are keeping it as a staging copy.
EOF
