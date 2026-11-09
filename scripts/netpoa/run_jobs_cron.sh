#!/usr/bin/env bash
# cPanel cron entry for ClipCash background jobs (email, schedules, expiries).
#
#   * * * * * /bin/bash /home/<user>/<app root>/scripts/netpoa/run_jobs_cron.sh /home/<user>/<venv>/bin/python
#
# The single argument is the virtual environment's python (cPanel shows the path
# on the "Setup Python App" page). Settings and secrets come from the private
# .env in the application root, never from the crontab line.
#
# Safety:
# * Bounded: run_jobs stops claiming work after CLIPCASH_JOBS_MAX_SECONDS
#   (default 45) so a run normally ends before the next minute starts.
# * No overlap: a non-blocking flock skips this minute if the previous run is
#   still busy. Without flock, overlapping runs are still safe (jobs are claimed
#   with SKIP LOCKED and leases) but use more processes.
# * Quiet: output goes to a private log (mode 600) outside the web root, so cron
#   sends no email every minute. The log is rotated at about 5 MB.
# * Duplicate email: delivery is at-least-once. A duplicate is possible only if
#   the host kills a run after the mail server accepted a message and before
#   success was recorded (docs/architecture/background-processing.md).
set -uo pipefail

python_bin="${1:-}"
if [ -z "$python_bin" ] || [ ! -x "$python_bin" ]; then
    echo "usage: run_jobs_cron.sh <virtualenv python>" >&2
    exit 2
fi

app_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
log_dir="${CLIPCASH_LOG_DIR:-$HOME/clipcash/logs}"
max_seconds="${CLIPCASH_JOBS_MAX_SECONDS:-45}"
case "$max_seconds" in
    ''|*[!0-9]*) echo "CLIPCASH_JOBS_MAX_SECONDS must be a whole number of seconds" >&2; exit 2 ;;
esac
if [ "$max_seconds" -lt 5 ] || [ "$max_seconds" -gt 55 ]; then
    echo "CLIPCASH_JOBS_MAX_SECONDS must be between 5 and 55 for a one-minute cron" >&2
    exit 2
fi

umask 077
mkdir -p "$log_dir"
log_file="$log_dir/run_jobs.log"
if [ -f "$log_file" ] && [ "$(wc -c < "$log_file")" -gt 5000000 ]; then
    mv -f "$log_file" "$log_file.1"
fi

if command -v flock >/dev/null 2>&1; then
    exec 9>"$log_dir/run_jobs.lock"
    if ! flock -n 9; then
        exit 0  # The previous run is still working; it will pick up this minute's jobs.
    fi
fi

cd "$app_dir" || exit 1
"$python_bin" -B manage.py run_jobs --max-seconds "$max_seconds" --worker-name "netpoa-cron" >>"$log_file" 2>&1
status=$?
if [ "$status" -ne 0 ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) run_jobs exited with status $status" >>"$log_file"
fi
exit "$status"
