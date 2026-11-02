#!/usr/bin/env bash
# Render Background Worker start command:  bash scripts/render/run_jobs_worker.sh
#
# Replaces the every-minute cron entry from docs/architecture/background-processing.md
# with a loop of the same bounded `run_jobs` command; the job system itself is
# unchanged. Overlapping or repeated runs are safe (jobs are claimed with SKIP
# LOCKED). On SIGTERM (deploy, restart) the loop stops after the current batch;
# if the platform kills the batch first, its job lease expires and the job is
# retried by the next run.
set -uo pipefail

PAUSE_SECONDS="${JOBS_WORKER_PAUSE_SECONDS:-10}"
# Stop claiming new jobs after this long, so a batch normally ends inside
# Render's shutdown grace period.
BATCH_SECONDS="${JOBS_WORKER_BATCH_SECONDS:-20}"
WORKER_NAME="render-${RENDER_INSTANCE_ID:-${HOSTNAME:-worker}}"

stopping=0
trap 'stopping=1' TERM INT

while [ "$stopping" -eq 0 ]; do
    python manage.py run_jobs --max-seconds "$BATCH_SECONDS" --worker-name "$WORKER_NAME" &
    batch=$!
    # A trapped signal interrupts `wait`; keep waiting so the batch can finish.
    while ! wait "$batch"; do
        if ! kill -0 "$batch" 2>/dev/null; then
            echo "run_jobs batch exited with a failure status" >&2
            break
        fi
    done
    [ "$stopping" -eq 0 ] || break
    sleep "$PAUSE_SECONDS" &
    wait $! || true
done
echo "run_jobs worker stopped"
