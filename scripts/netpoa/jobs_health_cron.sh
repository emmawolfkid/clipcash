#!/usr/bin/env bash
# cPanel cron health check for ClipCash background jobs. Silent while healthy.
#
#   */10 * * * * /bin/bash /home/<user>/<app root>/scripts/netpoa/jobs_health_cron.sh /home/<user>/<venv>/bin/python
#
# Set the cPanel "Cron Email" address: cron emails whatever this prints, and it
# prints only when `manage.py jobs_status` reports a problem (stale worker
# heartbeat, old backlog, expired leases, cache unavailable). The report holds
# job counts and sanitized error classes, never credentials or email content.
#
# Optional dead-man's switch: if CLIPCASH_HEARTBEAT_URL is set (for example a
# check URL from an external uptime service), it is requested only when the jobs
# are healthy. That service then alerts when the pings stop, which also covers
# cron itself stopping, which no check inside cron can detect. Keep that URL out
# of version control: put it in front of the command on the crontab line, e.g.
#   */10 * * * * CLIPCASH_HEARTBEAT_URL=https://... /bin/bash .../jobs_health_cron.sh ...
set -uo pipefail

python_bin="${1:-}"
if [ -z "$python_bin" ] || [ ! -x "$python_bin" ]; then
    echo "usage: jobs_health_cron.sh <virtualenv python>" >&2
    exit 2
fi

app_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$app_dir" || exit 1

report="$("$python_bin" -B manage.py jobs_status 2>&1)"
status=$?
if [ "$status" -ne 0 ]; then
    echo "ClipCash background jobs are UNHEALTHY (jobs_status exit $status) at $(date -u +%Y-%m-%dT%H:%M:%SZ)."
    echo "Check the cron job, $HOME/clipcash/logs/run_jobs.log and /manage/health/."
    echo
    echo "$report"
    exit "$status"
fi

if [ -n "${CLIPCASH_HEARTBEAT_URL:-}" ] && command -v curl >/dev/null 2>&1; then
    curl -fsS -m 10 --retry 2 -o /dev/null "$CLIPCASH_HEARTBEAT_URL" || true
fi
exit 0
