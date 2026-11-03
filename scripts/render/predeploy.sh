#!/usr/bin/env bash
# Render pre-deploy command:  bash scripts/render/predeploy.sh
#
# Applies migrations and creates the database cache table. Runs once per
# deploy, after the build and before the new instances receive traffic; a
# failure stops the deploy and the previous version keeps serving.
# Requires DATABASE_URL. Never fakes, resets or rolls back migrations.
#
# Database role separation (scripts/database/roles, audit CC-005): when
# MIGRATION_DATABASE_URL is set, schema changes run as that migration identity
# (clipcash_migrator, acting as clipcash_owner) while the web service and worker
# keep using the least-privileged runtime DATABASE_URL (clipcash_app). After a
# deploy that adds tables, re-run scripts/database/roles/03_grant_runtime.sql
# with the migration credential.
set -euo pipefail

if [ -n "${MIGRATION_DATABASE_URL:-}" ]; then
    echo "Running migrations with the migration database identity."
    export DATABASE_URL="$MIGRATION_DATABASE_URL"
fi

python manage.py migrate --noinput
# Idempotent: creates the DJANGO_CACHE_URL=dbcache://... table only if missing.
python manage.py createcachetable
