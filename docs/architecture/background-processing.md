# Background processing, email outbox and shared cache (Phase 02D)

Status: implemented. Runs entirely on PostgreSQL and cron: no Redis, message
broker or long-running process is required. No payment, reward or withdrawal
processing exists; the financial feature flags stay locked off.

## Job queue (`apps/core/jobs`, model `core.Job`)

| Aspect | Design |
| --- | --- |
| Identity | UUID; optional unique idempotency key (a repeat returns the existing job) |
| Types | Strict registry (`registry.register`). A stored job names a registered type; nothing in the database can make arbitrary code run |
| Payload | JSON validated against the type's schema: UUIDs, strings ≤ 200, integers, booleans. Identifiers only, never secrets or content |
| States | `PENDING → RUNNING → SUCCEEDED`; `RUNNING → RETRY_WAIT → RUNNING`; `→ FAILED` (terminal); `CANCELLED` (administrator) |
| Scheduling | `run_after`, `priority` (lower first) |
| Claiming | `SELECT … FOR UPDATE SKIP LOCKED` in a short transaction: concurrent workers never claim the same job (tested with 4 workers × 40 jobs and 6 workers × 1 job) |
| Leases | A claim sets `lease_expires_at` (`JOBS_LEASE_SECONDS`, default 300). The job row stays locked while its handler runs; lease recovery skips locked rows, so only jobs whose worker died are reclaimed |
| Retries | Exponential backoff with equal jitter (`base · 2^(attempt−1)`, capped, half fixed + half random), up to `max_attempts` |
| Errors | `RetryableJobError` (transient), `PermanentJobError` (fail now), anything else is `UNEXPECTED` (retried). Only messages from these ClipCash errors are stored; other exceptions keep only their class name, so addresses, URLs and tokens never reach the database |
| Interruption | Ctrl+C or `SystemExit` returns the job to `RETRY_WAIT`; a hard kill is recovered when the lease expires |
| Correlation | `request_id` from the enqueueing request or command |

### At-least-once execution

A worker can crash **after** an external action succeeds (e.g. the SMTP server
accepted an email) but **before** it records success. The lease then expires
and the job runs again. Exactly-once is impossible without the external system's
cooperation, so:

* every handler must be idempotent (the email handler re-checks the outbox row's
  status under lock and does nothing if it is no longer pending);
* future external effects (payments, payouts) must use provider-side
  idempotency keys and be reconciled against provider records.

## Durable email outbox (`apps/notifications`)

* `enqueue_email()` writes an `OutboundEmail` row and its delivery job **in the
  caller's transaction**. A rolled-back registration or account change leaves
  neither, so nothing is ever sent for it (tested). Nothing is sent inside the
  web request.
* **No tokens or rendered content are stored.** The outbox row holds only the
  kind, recipient, account id, `base_url` and the request time
  (`token_issued_at`). The builder in `apps/accounts/emails.py` renders the
  message at delivery, reloading the account and generating the link token
  **dated to `token_issued_at`**. Consequences:
  * tokens exist only in the delivered email: not in the database, job
    payloads, audit records, admin pages or error summaries (tested);
  * delays and retries never extend a link's validity: its age counts from the
    request (tested);
  * single-use behaviour is unchanged (tokens are bound to account state).
* `expires_at` = request time + the link lifetime. A message not delivered in
  time becomes `EXPIRED` and is never sent, even after an administrator retry.
* A message that no longer applies becomes `SKIPPED` with a reason, never sent:
  the account was activated, disabled, deleted or changed its address.
* SMTP errors: recipient or sender refused, and 5xx data errors, are permanent;
  connection problems and 4xx replies are retried (6 attempts, 1 min to 1 h
  backoff). On terminal failure the email is marked `FAILED` and audited.
  Delivery failures never change account state.
* Any Django email backend works (`DJANGO_EMAIL_BACKEND`).

## Shared cache and rate limiting

| Need | Production configuration |
| --- | --- |
| Shared cache (site configuration, health probe) | `DJANGO_CACHE_URL=dbcache://clipcash_cache`, then `python manage.py createcachetable`. Works on any PostgreSQL host; Redis is not assumed |
| Rate limiting | `DJANGO_THROTTLE_BACKEND=database` (the default when `DEBUG` is off): atomic `INSERT … ON CONFLICT DO UPDATE` counters in `core_ratelimitcounter`. Django's database cache cannot increment atomically, so it is **not** used for counters (`check --deploy` warns `core.W001` otherwise) |
| Key namespace | `KEY_PREFIX` = `clipcash` (`DJANGO_CACHE_KEY_PREFIX`), explicit default TTL 300 s; rate-limit keys are SHA-256 digests (no emails or IPs stored) |
| Fail-closed | Rate-limit storage errors treat the request as limited; a per-process cache in production fails startup (`accounts.E001`) |
| Invalidation | Site configuration cache is dropped on update and again after commit |
| Monitoring | Scheduled `core.health_check` probes the cache; `jobs_status` probes it live |

Ledger balances are never cached: the database is the only authoritative source.

## Scheduled tasks and commands

Cron runs one command every minute (each run is bounded):

```
* * * * *  cd /path/to/clipcash && venv/bin/python manage.py run_jobs
*/5 * * * * cd /path/to/clipcash && venv/bin/python manage.py jobs_status || <alert command>
```

`run_jobs [--max-seconds 50] [--max-jobs 100] [--worker-name NAME] [--no-schedule]`
records a heartbeat, recovers expired leases, enqueues due scheduled tasks, then
processes jobs until a limit is reached. Overlapping runs are safe.

| Scheduled task | Default interval | Purpose |
| --- | --- | --- |
| `core.health_check` | 5 min | Cache probe recorded on the worker heartbeat |
| `core.detect_stale_jobs` | 5 min | Logs overdue jobs and expired leases |
| `core.prune_rate_limits` | 1 h | Deletes expired counters |
| `core.prune_finished_jobs` | 1 day | Deletes succeeded/cancelled jobs after 30 days, failed after 90 |
| `notifications.prune_outbox` | 1 day | Deletes non-pending emails after 90 days |

Intervals: `DJANGO_JOBS_SCHEDULE_INTERVALS=core.health_check=600,...` (minimum
60 s). Each due slot is enqueued once (idempotency key per slot). Tasks can be
paused in the admin (audited).

`jobs_status` prints JSON (status counts, oldest due job, heartbeat age, failed
in the last 24 h, expired leases, cache health) and exits 1 when unhealthy:
stale heartbeat (`JOBS_HEARTBEAT_STALE_SECONDS`, 300), backlog older than
`JOBS_BACKLOG_ALERT_SECONDS` (900), expired leases or cache unavailable.

## Operations workspace (admin)

| Screen | Access | Actions |
| --- | --- | --- |
| Background jobs | `core.view_operations` | Retry failed/cancelled (`core.retry_job`), cancel pending/waiting/failed (`core.cancel_job`) |
| Worker heartbeats | `core.view_operations` | None |
| Scheduled tasks | `core.view_operations` | Enable/pause (`core.manage_schedules`) |
| Outbound emails | `core.view_operations` | None; recipient masked, no content stored |

Super Administrators hold these implicitly; anyone else must be granted them
explicitly. Actions are validated by the services (ineligible jobs are
refused) and audited. Running jobs cannot be cancelled. Some job types (the
maintenance tasks) cannot be retried manually. Jobs cannot be created, edited
or deleted from the admin.

## Audit integration

| Event | When | Transaction |
| --- | --- | --- |
| `jobs.job.failed` | A job reaches `FAILED` | Same transaction as the status change |
| `notifications.email.failed` | An email delivery fails terminally | Same transaction |
| `jobs.job.retried`, `jobs.job.cancelled` | Administrator actions | Same transaction |
| `jobs.schedule.changed` | Schedule enabled or paused | Same transaction |

Routine successes and individual retries are not audited (they are logged to
`clipcash.jobs`), to avoid noise. Workers run inside `audit_scope(source="SYSTEM")`
with a per-run correlation id.

## Hosting requirements (Netpoa: to confirm before deployment)

These have **not** been verified with Netpoa. The design works with the minimum
column; anything less needs a change of plan.

| Capability | Minimum needed | Why | Status |
| --- | --- | --- | --- |
| PostgreSQL access from cron jobs | Same database as the site | Queue, outbox, counters, cache | To confirm |
| Cron frequency | Every 1 minute (5 minutes workable, slower email) | Runs `run_jobs` | To confirm |
| Maximum script run time | ≥ 60 s | One bounded batch (default 50 s) | To confirm |
| Persistent worker processes | Not required | Optional: run `run_jobs` in a loop under a process manager if allowed | To confirm |
| Outgoing SMTP | Port 587 (or an HTTP email API) to a provider; sending limits per hour | Email outbox | To confirm |
| Database connections | Web workers + 1–2 concurrent `run_jobs` | `CONN_MAX_AGE` = 60 holds connections | To confirm |
| Cache options | PostgreSQL only is enough; Redis/Memcached optional | `dbcache://` | To confirm |
| `createcachetable` at deploy | One-time command | Database cache table | Part of deploy procedure |

If cron is limited to every 5 minutes or scripts are killed before 60 s, lower
`DJANGO_JOBS_MAX_RUN_SECONDS` and expect email latency up to the cron interval.

## Known limitations

* A handler that runs longer than its lease is not killed (Python cannot safely
  interrupt it); keep handlers well under `JOBS_LEASE_SECONDS`.
* Delivery latency equals the cron interval.
* Email bodies are plain text; no bounce or complaint processing yet (needs the
  email provider's webhooks).
* The worker heartbeat proves cron is running only while a monitor checks
  `jobs_status`; alert wiring is a deployment task (Phase 16).
