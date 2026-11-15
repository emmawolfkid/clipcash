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
# Tools: MARIADB_DUMP / MARIADB_CLIENT override the client programs (default: mariadb-dump /
# mariadb, else mysqldump / mysql). CLIPCASH_ALLOW_OPEN_OPTION_FILE=1 skips the file-mode check
# (only for Windows/Git Bash drills, where modes are not meaningful).
set -euo pipefail

out_dir="${1:-}"
option_file="${CLIPCASH_DB_OPTION_FILE:-}"
database="${CLIPCASH_DB_NAME:-}"
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
trap 'rm -f "$partial"' EXIT

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
trap - EXIT

version="$(sql 'SELECT VERSION()')"
server_triggers="$(sql 'SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()')"
table_names="$(sql "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME")"
manifest_partial="$out_abs/$base.manifest.partial"
trap 'rm -f "$manifest_partial"' EXIT
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
mv "$manifest_partial" "$out_abs/$base.manifest"
trap - EXIT

(cd "$out_abs" && sha256sum "$base.sql.gz" "$base.manifest" > "$base.sha256")
echo "Backup written: $file"
echo "  tables: $tables, triggers: $triggers, size: $(du -h "$file" | cut -f1)"
echo "  manifest: $base.manifest, checksums: $base.sha256"
echo "Copy all three files off the server (encrypted) and run restore_check.sh on a scratch database."
