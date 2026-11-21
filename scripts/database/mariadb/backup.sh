#!/usr/bin/env bash
# PRD-00B: logical backup of the ClipCash MariaDB database.
#
#   CLIPCASH_DB_OPTION_FILE=<path to backup.cnf> CLIPCASH_DB_NAME=<database> \
#       bash scripts/database/mariadb/backup.sh <output directory>
#
# The option file holds the credentials, never the command line, the environment or a log:
#
#   [client]
#   user=<migration login>          (it owns the triggers, so the dump can include them)
#   password=<its password>
#   host=localhost
#
# It must be private (mode 600 or 400) and outside the repository and the web root.
#
# What it does, read-only for the database:
# * one consistent InnoDB snapshot (--single-transaction): no table locks, the site stays up;
# * tables, data, triggers (the financial and audit guards) and views, utf8mb4, hex BLOBs;
#   no DROP statements, so a restore can only ever go into an EMPTY database;
# * writes clipcash-<database>-<UTC time>.sql.gz, a .sha256 file and a .manifest file
#   (table and trigger counts, row counts per table taken just after the snapshot);
# * refuses an output directory inside the repository: dumps hold member data and must
#   never be committed. Keep copies encrypted and off the server (see the runbook).
# * fails if the dump is incomplete (no "Dump completed" trailer) or has no data or triggers.
#
# PRD-04: payment receipts are FILES, not database rows. Set CLIPCASH_EVIDENCE_ROOT to the private
# evidence directory (DJANGO_PAYMENTS_EVIDENCE_ROOT) and the backup also writes
# clipcash-<database>-<UTC time>.evidence.tar.gz, taken AFTER the database snapshot, so it holds every
# file the dump refers to (files are never changed or deleted; a newer one in it is harmless). It is
# listed in the .sha256 file and counted in the manifest. Required once manual payments are live.
#
# Tools: MARIADB_DUMP / MARIADB_CLIENT override the client programs (default: mariadb-dump /
# mariadb, else mysqldump / mysql). CLIPCASH_ALLOW_OPEN_OPTION_FILE=1 skips the file-mode check
# (only for Windows/Git Bash drills, where modes are not meaningful).
set -euo pipefail

out_dir="${1:-}"
option_file="${CLIPCASH_DB_OPTION_FILE:-}"
database="${CLIPCASH_DB_NAME:-}"
evidence_root="${CLIPCASH_EVIDENCE_ROOT:-}"
if [ -z "$out_dir" ] || [ -z "$option_file" ] || [ -z "$database" ]; then
    echo "usage: CLIPCASH_DB_OPTION_FILE=... CLIPCASH_DB_NAME=... bash scripts/database/mariadb/backup.sh <output directory>" >&2
    exit 2
fi
case "$database" in
    *[!A-Za-z0-9_]*) echo "CLIPCASH_DB_NAME may contain only letters, digits and underscores." >&2; exit 2 ;;
esac

repo_root="$(realpath "$(dirname "$0")/../../..")"
inside_repo() { case "$(realpath -m "$1")/" in "$repo_root"/*) return 0 ;; esac; return 1; }

if [ ! -f "$option_file" ]; then
    echo "Option file not found: $option_file" >&2
    exit 2
fi
if inside_repo "$option_file"; then
    echo "Refusing: the option file is inside the repository." >&2
    exit 2
fi
mode="$(stat -c '%a' "$option_file" 2>/dev/null || echo unknown)"
if [ "${CLIPCASH_ALLOW_OPEN_OPTION_FILE:-0}" != "1" ] && [ "$mode" != "600" ] && [ "$mode" != "400" ]; then
    echo "Refusing: the option file must be private (chmod 600), not mode $mode." >&2
    exit 2
fi
if inside_repo "$out_dir"; then
    echo "Refusing: $(realpath -m "$out_dir") is inside the repository. Choose a directory outside it." >&2
    exit 2
fi
if [ -n "$evidence_root" ]; then
    if [ ! -d "$evidence_root" ]; then
        echo "CLIPCASH_EVIDENCE_ROOT is not a directory: $evidence_root" >&2
        exit 2
    fi
    case "$(realpath -m "$out_dir")/" in "$(realpath "$evidence_root")"/*)
        echo "Refusing: the output directory is inside the evidence directory." >&2; exit 2 ;;
    esac
fi

pick() { for tool in "$@"; do command -v "$tool" >/dev/null 2>&1 && { echo "$tool"; return; }; done; echo "$1"; }
DUMP="${MARIADB_DUMP:-$(pick mariadb-dump mysqldump)}"
CLIENT="${MARIADB_CLIENT:-$(pick mariadb mysql)}"
# Output without carriage returns (Windows clients print CRLF); a failed query fails the backup.
sql() { "$CLIENT" --defaults-extra-file="$option_file" --batch --skip-column-names "$database" -e "$1" | tr -d '\r'; }

umask 077
out_abs="$(realpath -m "$out_dir")"
mkdir -p "$out_abs"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
base="clipcash-$database-$stamp"
file="$out_abs/$base.sql.gz"
partial="$file.partial"
manifest_partial="$out_abs/$base.manifest.partial"
evidence="$out_abs/$base.evidence.tar.gz"
# A backup is complete or absent: any failure removes every file this run produced.
completed=0
cleanup() {
    [ "$completed" = "1" ] || rm -f "$partial" "$file" "$manifest_partial" "$out_abs/$base.manifest" \
        "$evidence.partial" "$evidence" "$out_abs/$base.sha256"
}
trap cleanup EXIT

"$DUMP" --defaults-extra-file="$option_file" --single-transaction --quick --triggers --skip-routines \
    --skip-events --no-tablespaces --hex-blob --default-character-set=utf8mb4 --skip-add-drop-table \
    "$database" | gzip -9 > "$partial"

if ! gzip -dc "$partial" | tail -n 5 | grep -q "^-- Dump completed"; then
    echo "Backup is incomplete (no 'Dump completed' trailer); treating as failed." >&2
    exit 1
fi
tables="$(gzip -dc "$partial" | grep -c '^CREATE TABLE' || true)"
inserts="$(gzip -dc "$partial" | grep -c '^INSERT INTO' || true)"
triggers="$(gzip -dc "$partial" | grep -c 'CREATE\*/ /\*!50017 DEFINER=.*TRIGGER\|^/\*!50003 CREATE.*TRIGGER' || true)"
if [ "$tables" -eq 0 ] || [ "$inserts" -eq 0 ] || [ "$triggers" -eq 0 ]; then
    echo "Backup has $tables tables, $inserts INSERT statements and $triggers triggers; treating as failed." >&2
    exit 1
fi
mv "$partial" "$file"

version="$(sql 'SELECT VERSION()')"
server_triggers="$(sql 'SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()')"
table_names="$(sql "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME")"
{
    echo "# ClipCash MariaDB backup manifest. Row counts were read just after the dump's snapshot;"
    echo "# on a live site they can differ slightly from the dump. Compare with restore_check.sh output."
    echo "database=$database"
    echo "created_utc=$stamp"
    echo "server_version=$version"
    echo "tables_in_dump=$tables"
    echo "triggers_in_dump=$triggers"
    echo "triggers_on_server=$server_triggers"
} > "$manifest_partial"
for table in $table_names; do
    count="$(sql "SELECT COUNT(*) FROM \`$table\`")"  # set -e: a failed count fails the backup.
    case "$count" in ''|*[!0-9]*) echo "Could not count rows in $table; treating as failed." >&2; exit 1 ;; esac
    echo "rows.$table=$count" >> "$manifest_partial"
done
checksummed=("$base.sql.gz")
if [ -n "$evidence_root" ]; then
    # After the snapshot: a superset of the files the dump refers to. The archive goes through
    # stdout/stdin, so no path is ever read as tar's "host:file" remote-archive syntax.
    tar -C "$evidence_root" -czf - . > "$evidence.partial"
    listing="$(tar -tzf - < "$evidence.partial")"  # set -e: an unreadable archive fails the backup.
    evidence_files="$(printf '%s\n' "$listing" | grep -vc '/$' || true)"
    mv "$evidence.partial" "$evidence"
    evidence_rows=0
    if [ "$(sql "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'payments_manualpaymentevidence'")" = "1" ]; then
        evidence_rows="$(sql 'SELECT COUNT(*) FROM payments_manualpaymentevidence')"
    fi
    {
        echo "evidence_files=$evidence_files"
        echo "evidence_rows_after_archive=$evidence_rows"
    } >> "$manifest_partial"
    checksummed+=("$base.evidence.tar.gz")
fi
mv "$manifest_partial" "$out_abs/$base.manifest"
(cd "$out_abs" && sha256sum "${checksummed[@]}" "$base.manifest" > "$base.sha256")
completed=1
echo "Backup written: $file"
echo "  tables: $tables, triggers: $triggers, size: $(du -h "$file" | cut -f1)"
if [ -n "$evidence_root" ]; then
    echo "  payment evidence: $base.evidence.tar.gz ($evidence_files files)"
else
    echo "  payment evidence: NOT included (set CLIPCASH_EVIDENCE_ROOT once manual payments are live)"
fi
echo "  manifest: $base.manifest, checksums: $base.sha256"
echo "Copy all of these files off the server (encrypted) and run restore_check.sh on a scratch database."
