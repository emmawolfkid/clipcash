#!/usr/bin/env bash
# Logical backup of the ClipCash database (audit CC-006 / CC-017).
#
#   BACKUP_DATABASE_URL="<connection URL>" bash scripts/database/backup.sh <output directory>
#
# * Writes clipcash-<UTC timestamp>.dump (pg_dump custom format) and a .sha256 file.
# * Refuses to write inside the repository: dumps contain member data and must
#   never be committed. Store them encrypted, off the database host.
# * Validates the archive (pg_restore --list) before reporting success.
# * Never prints the connection URL. Read-only for the database.
# Set PG_DUMP / PG_RESTORE to the client binaries if they are not on PATH
# (e.g. "/c/Program Files/PostgreSQL/18/bin/pg_dump.exe").
set -euo pipefail

out_dir="${1:-}"
if [ -z "$out_dir" ]; then
    echo "usage: BACKUP_DATABASE_URL=... bash scripts/database/backup.sh <output directory>" >&2
    exit 2
fi
url="${BACKUP_DATABASE_URL:-}"
if [ -z "$url" ]; then
    echo "BACKUP_DATABASE_URL is not set." >&2
    exit 2
fi
PG_DUMP="${PG_DUMP:-pg_dump}"
PG_RESTORE="${PG_RESTORE:-pg_restore}"

# Decide before creating anything: the directory must not be inside the repository.
out_abs="$(realpath -m "$out_dir")"
repo_root="$(realpath "$(dirname "$0")/../..")"
case "$out_abs/" in
    "$repo_root"/*)
        echo "Refusing: $out_abs is inside the repository. Choose a directory outside it." >&2
        exit 2
        ;;
esac
mkdir -p "$out_abs"

umask 077
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
file="$out_abs/clipcash-$stamp.dump"

"$PG_DUMP" --format=custom --no-owner --no-privileges --file "$file" --dbname "$url"
listing="$("$PG_RESTORE" --list "$file")"
tables="$(printf '%s\n' "$listing" | grep -c ' TABLE DATA ' || true)"
triggers="$(printf '%s\n' "$listing" | grep -c ' TRIGGER ' || true)"
if [ "$tables" -eq 0 ]; then
    echo "Backup archive contains no table data; treating as failed: $file" >&2
    exit 1
fi
(cd "$out_abs" && sha256sum "$(basename "$file")" > "$(basename "$file").sha256")
echo "Backup written: $file"
echo "  table data sections: $tables, triggers: $triggers, checksum: $(basename "$file").sha256"
