#!/usr/bin/env bash
# Render pre-deploy command:  bash scripts/render/predeploy.sh
#
# Applies migrations and creates the database cache table. Runs once per
# deploy, after the build and before the new instances receive traffic; a
# failure stops the deploy and the previous version keeps serving.
# Requires DATABASE_URL. Never fakes, resets or rolls back migrations.
set -euo pipefail

python manage.py migrate --noinput
# Idempotent: creates the DJANGO_CACHE_URL=dbcache://... table only if missing.
python manage.py createcachetable
