#!/usr/bin/env bash
# Restore drill: restore a backup into an EMPTY scratch database and verify it.
#
#   RESTORE_DATABASE_URL="<URL of an empty database named clipcash_restore_...>" \
#       bash scripts/database/restore_check.sh <file.dump>
#
# Safety: refuses unless the target database name starts with clipcash_restore_
# and it has no tables, so it can never overwrite the application database.
# Verifies the checksum (if present), restores, and checks that the financial and
# audit lock triggers and constraints came back. Never prints the connection URL.
# Afterwards, run the application against the copy (never production):
#   DATABASE_URL="<restore URL>" python manage.py showmigrations
# and apply scripts/database/roles/02-03 if role separation is used.
set -euo pipefail

file="${1:-}"
url="${RESTORE_DATABASE_URL:-}"
if [ -z "$file" ] || [ -z "$url" ]; then
    echo "usage: RESTORE_DATABASE_URL=... bash scripts/database/restore_check.sh <file.dump>" >&2
    exit 2
fi
PSQL="${PSQL:-psql}"
PG_RESTORE="${PG_RESTORE:-pg_restore}"

db="$("$PSQL" "$url" -Atc "SELECT current_database()")"
case "$db" in
    clipcash_restore_*) ;;
    *) echo "Refusing: target database '$db' does not start with clipcash_restore_." >&2; exit 2 ;;
esac
existing="$("$PSQL" "$url" -Atc "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
if [ "$existing" != "0" ]; then
    echo "Refusing: target database '$db' is not empty ($existing tables)." >&2
    exit 2
fi
if [ -f "$file.sha256" ]; then
    (cd "$(dirname "$file")" && sha256sum --check --quiet "$(basename "$file").sha256")
fi

"$PG_RESTORE" --no-owner --no-privileges --exit-on-error --dbname "$url" "$file"

"$PSQL" "$url" -v ON_ERROR_STOP=1 -At <<'SQL'
SELECT 'lock triggers present: ' || count(*) || ' of 13' FROM pg_trigger WHERE NOT tgisinternal AND tgenabled = 'O'
  AND tgname IN ('ledger_account_guard', 'ledger_line_apply', 'ledger_entry_immutable', 'ledger_line_immutable',
                 'ledger_line_balanced', 'ledger_entry_complete', 'a_wallets_block_member_postings',
                 'wallets_transactions_locked', 'withdrawals_request_locked', 'advertising_campaign_guard',
                 'payments_intent_guard', 'membership_subscription_guard', 'audit_auditevent_append_only');
SELECT 'lock constraints present: ' || count(*) || ' of 2' FROM pg_constraint
  WHERE conname IN ('adv_campaign_funding_locked', 'sub_activation_source_consistent');
SELECT 'migrations recorded: ' || count(*) FROM django_migrations;
SELECT 'accounts: ' || count(*) FROM accounts_user;
SELECT 'audit events: ' || count(*) FROM audit_auditevent;
SELECT 'journal entries: ' || count(*) FROM wallets_journalentry;
SQL
echo "Restore into '$db' completed. Compare the counts above with the source before relying on this backup."
