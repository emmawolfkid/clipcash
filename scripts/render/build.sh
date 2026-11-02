#!/usr/bin/env bash
# Render build command:  bash scripts/render/build.sh
#
# Installs pinned dependencies, collects static files and runs the deployment
# system checks. It does NOT touch the database: migrations run separately
# (scripts/render/predeploy.sh). See docs/deployment/render.md.
set -euo pipefail

python -m pip install --requirement requirements/base.txt

python manage.py collectstatic --noinput

# Fails the build on any error-level check, e.g. sandbox payments or the demo
# video provider enabled outside an approved isolated deployment.
python manage.py check --deploy --fail-level ERROR
