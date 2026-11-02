# Deploying ClipCash on Render

Status: **prepared, not deployed.** This guide configures a Render Web Service
connected to the GitHub repository `emmawolfkid/clipcash` and the Render
PostgreSQL database `clipcash-db`.

> **ClipCash is not approved for real-money operation.** Deploying does not
> change that. Live payments, real-money rewards and withdrawals are locked off
> in code (`apps/core/features.py`) and by database triggers; there is no live
> payment or payout provider to enable. Payments default to `disabled` outside
> `DEBUG`, and the build fails (`payments.E002`) if sandbox payments are switched
> on without the explicit isolated-demo flag. Do not set
> `DJANGO_PAYMENTS_SANDBOX_ALLOWED` or `VIDEO_DEMO_ALLOWED` on a public deployment.

## 1. Architecture on Render

| Component | Render resource | Required? | Plan notes |
| --- | --- | --- | --- |
| Django site (Gunicorn + WhiteNoise) | Web Service, Python runtime | Yes | Free works for a trial; paid needed for pre-deploy, shell, SMTP and no spin-down (§2) |
| Database | Render PostgreSQL `clipcash-db` | Yes | Free expires after 30 days (§10) |
| Background jobs (`run_jobs`) | Background Worker | **Yes, for a usable site** | **Paid only** (§8) |
| Shared cache, rate-limit counters, job queue | The same PostgreSQL database | Yes | No Redis or Key Value instance needed |
| Video hosting | Bunny Stream (external) | For video features | Separate Bunny account |
| Outgoing email | An SMTP provider (external) | For registration | Paid Render instance needed for SMTP ports (§9) |

Static files are served by the web service itself (WhiteNoise); there is no
media storage to configure, because video uploads go directly to Bunny Stream.

No `render.yaml` Blueprint is included on purpose: syncing a Blueprint creates
or attaches every resource it declares (including a paid worker) and would try
to manage `clipcash-db`, which was created separately. Configure the services
in the dashboard as described below; a Blueprint can be added later once the
plans are approved.

## 2. Choosing plans (costs)

Prices change; confirm on <https://render.com/pricing> before creating anything.

| Need | Free web service | Paid web service (Starter or higher) |
| --- | --- | --- |
| Serves the site | Yes, but spins down after 15 min idle (~1 min cold start) | Always on |
| Pre-deploy command (migrations) | **No**; migrate manually (§7.2) | Yes |
| Dashboard shell (`createsuperuser`, `jobs_status`) | **No** | Yes |
| Outbound SMTP (ports 465/587) | **Blocked** by Render | Allowed (port 25 is blocked everywhere) |

Background Workers and Cron Jobs have no free tier. **Minimum for a usable
public site: a paid web service and a paid background worker**, plus a paid
database for anything kept longer than 30 days. A free web service + free
database is suitable only for a short, private smoke test (no email, so nobody
can complete registration).

## 3. Repository files used by Render

| File | Purpose |
| --- | --- |
| `.python-version` | `3.13`: Render installs the latest 3.13.x. Without it, new services default to Python 3.14 |
| `requirements/base.txt` | Pinned runtime dependencies, including `gunicorn` and `whitenoise` |
| `scripts/render/build.sh` | Install, `collectstatic`, `check --deploy` (no database access) |
| `scripts/render/predeploy.sh` | `migrate --noinput` and `createcachetable` |
| `config/gunicorn.conf.py` | Binds `0.0.0.0:$PORT`, `WEB_CONCURRENCY` sync workers (default 2), 60 s timeout, access log off |
| `scripts/render/run_jobs_worker.sh` | Background Worker loop around `manage.py run_jobs` |

## 4. Environment variables

Set these on the Web Service **and** the Background Worker (an Environment
Group shared by both is the simplest). Values below are safe examples; generate
or copy real values in the dashboard and never commit them.

### Required

| Variable | Example / value | Notes |
| --- | --- | --- |
| `DATABASE_URL` | *(paste the Internal Database URL of `clipcash-db`)* | Dashboard → `clipcash-db` → Connect → **Internal**. Never invent or commit it |
| `DJANGO_SECRET_KEY` | *(use Render's "Generate" button)* | At least 50 random characters; never reuse the local key |
| `DJANGO_DEBUG` | `False` | |
| `DJANGO_DB_SSLMODE` | `require` | Encrypted database connection. See §5 |
| `DJANGO_CACHE_URL` | `dbcache://clipcash_cache` | Shared cache; table created by `predeploy.sh` |
| `DJANGO_SECURE_PROXY_SSL_HEADER` | `True` | Render terminates HTTPS and sets `X-Forwarded-Proto` |
| `DJANGO_SECURE_SSL_REDIRECT` | `True` | Redirects HTTP to HTTPS (the health check is exempt) |
| `DJANGO_SECURE_HSTS_SECONDS` | `0` | Keep 0 until HTTPS is verified (§12) |
| `DJANGO_PAYMENTS_MODE` | `disabled` | Already the default outside `DEBUG`; set explicitly for clarity |
| `DJANGO_DEFAULT_FROM_EMAIL` | `ClipCash <no-reply@your-domain.example>` | A sender your email provider has verified |

`RENDER_EXTERNAL_HOSTNAME` is set by Render automatically; the settings add it
to `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` (as `https://...`). Do not set it
yourself.

### Required when used

| Variable | Example | Notes |
| --- | --- | --- |
| `DJANGO_ALLOWED_HOSTS` | `clipcash.example,www.clipcash.example` | Custom domains only. **Set it**: the default is `127.0.0.1,localhost` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://clipcash.example,https://www.clipcash.example` | Custom domains only |
| `DJANGO_EMAIL_BACKEND` | `django.core.mail.backends.smtp.EmailBackend` | Default is the console backend (emails only appear in logs) |
| `DJANGO_EMAIL_HOST`, `DJANGO_EMAIL_PORT`, `DJANGO_EMAIL_USE_TLS` | `smtp.provider.example`, `587`, `True` | From the email provider |
| `DJANGO_EMAIL_HOST_USER`, `DJANGO_EMAIL_HOST_PASSWORD` | *(from the provider)* | Secret |
| `VIDEO_PROVIDER` | `bunny` | Default outside `DEBUG` |
| `BUNNY_STREAM_LIBRARY_ID`, `BUNNY_STREAM_CDN_HOSTNAME` | `123456`, `vz-xxxxxxxx.b-cdn.net` | Until set, `check --deploy` warns `videos.W001` and uploads/playback fail |
| `BUNNY_STREAM_API_KEY`, `BUNNY_STREAM_TOKEN_SECURITY_KEY` | *(from Bunny; two different secrets)* | Secret |
| `DJANGO_TRUSTED_PROXY_COUNT` | `1` (to be verified) | See §13, deployment blocker B3 |

### Optional

`WEB_CONCURRENCY` (Gunicorn workers, default 2), `GUNICORN_TIMEOUT` (60),
`GUNICORN_LOG_LEVEL` (`info`), `JOBS_WORKER_BATCH_SECONDS` (20),
`JOBS_WORKER_PAUSE_SECONDS` (10), and every optional variable in `.env.example`.

**Never set on a public deployment:** `DJANGO_PAYMENTS_SANDBOX_ALLOWED`,
`VIDEO_DEMO_ALLOWED`, `DJANGO_MEMBERSHIPS_COMPLIMENTARY_GRANTS` (unless a
business policy approves free memberships), `DJANGO_DEBUG=True`.

## 5. Connecting to `clipcash-db`

* Create the web service and worker **in the same region** as `clipcash-db`;
  the Internal Database URL only works inside that region's private network.
* Use the **Internal** URL for the services. The **External** URL is only for
  connecting from your own machine (e.g. a one-off migration on a free plan,
  §7.2) and requires TLS; restrict the database's inbound IP allow-list to your
  own address while doing that, then remove it.
* `DJANGO_DB_SSLMODE=require` encrypts the connection. If the first deploy
  fails with an SSL error on the internal connection, set it to `prefer`
  temporarily and raise the question with Render support;
  `check --deploy` then shows warning `core.W002` as a reminder. A `sslmode`
  in the URL query string is also honoured; `DJANGO_DB_SSLMODE` wins if both are set.
* The settings refuse any non-PostgreSQL `DATABASE_URL` at startup.
* Connections: each Gunicorn worker keeps one connection (`CONN_MAX_AGE` 60 s),
  plus one for the worker loop. Two web workers + one job worker use about 3
  connections, well within any Render plan.

**Audit integrity on a managed database.** Render gives one database user that
owns every table, and the append-only audit trigger can be bypassed by the
table owner. The role separation in `scripts/database/production_roles.example.sql`
(owner role for migrations, restricted role for the app) should be reviewed
with Render's user management before real data is stored. Until then, treat
audit immutability as protected against the application, not against someone
holding the database credentials.

## 6. Web Service configuration

Dashboard → New → Web Service → connect GitHub repository `emmawolfkid/clipcash`.

| Setting | Value |
| --- | --- |
| Name | `clipcash` (becomes `clipcash.onrender.com` if available) |
| Region | Same as `clipcash-db` |
| Branch | `main` |
| Root Directory | *(empty)* |
| Language / Runtime | Python 3 |
| Build Command | `bash scripts/render/build.sh` |
| Pre-Deploy Command (paid only) | `bash scripts/render/predeploy.sh` |
| Start Command | `gunicorn --config config/gunicorn.conf.py config.wsgi:application` |
| Health Check Path | `/healthz` |
| Instance type | See §2 |
| Auto-Deploy | `On Commit` once approved; **Off** for the first deploy (deploy manually) |
| Environment | Variables from §4 |

### Health check

`GET /healthz` returns `200 ok` when the process serves requests and
`SELECT 1` succeeds on the database, otherwise `503 unavailable` (the error class
is logged; host names and credentials are not). It is answered before host
validation, the HTTPS redirect, sessions and maintenance mode, so maintenance
mode never fails deploys. Render only routes traffic to a new deploy once it is
healthy, stops routing to an instance after 15 s of failures and restarts it
after 60 s.

### Static files

`build.sh` runs `collectstatic`; WhiteNoise serves `/static/` from the build
output with hashed file names (cached for a year, compressed with gzip). A
template that refers to a file missing from `static/` fails with a server
error in production, so check new static references locally with
`DJANGO_DEBUG=False python manage.py collectstatic` before pushing.

### Logging

All logs go to stdout/stderr (Render's Logs tab). Gunicorn access logs are
**off** because account activation, email confirmation and password reset URLs
contain single-use tokens; server errors are logged by `django.request` with
those token segments redacted. Secrets, `DATABASE_URL` and email addresses are
never logged by ClipCash code.

## 7. Database migrations

`build.sh` never touches the database. Migrations are reviewed and run deliberately.

### 7.1 Paid web service (recommended)

The Pre-Deploy Command runs `predeploy.sh` on each deploy, after the build and
before traffic moves. If it fails, the deploy stops and the previous version
keeps serving. Before any deploy that contains new migrations:

1. Review them locally: `python manage.py migrate --plan` against a copy of
   production data, and read each migration (several add triggers and
   constraints that validate existing rows).
2. Take a backup (§10) or confirm the latest one exists.
3. Deploy. Watch the pre-deploy log for `migrate` output.

### 7.2 Free web service

There is no pre-deploy command or shell. Run migrations from your own machine
against the **External** Database URL, before deploying code that needs them:

```powershell
# One PowerShell session; nothing is saved to disk. Paste the External URL when prompted.
$env:DATABASE_URL = Read-Host "External Database URL"
$env:DJANGO_DB_SSLMODE = "require"
$env:DJANGO_DEBUG = "False"
$env:DJANGO_CACHE_URL = "dbcache://clipcash_cache"
python manage.py migrate --plan
python manage.py migrate --noinput
python manage.py createcachetable
Remove-Item Env:DATABASE_URL
```

Your local `.env` still supplies `DJANGO_SECRET_KEY`; that is fine for running
migrations. Never put the External URL in `.env` or any committed file.

`btree_gist` (memberships migration 0001) is a trusted PostgreSQL extension,
so the database owner can create it; no superuser is needed.

### 7.3 First administrator

`python manage.py createsuperuser` is the only way to create a Super
Administrator. Paid: Web Service → Shell. Free: run it locally with the
External URL as in §7.2.

## 8. Background worker (required)

ClipCash's PostgreSQL job queue needs `run_jobs` to execute regularly. Locally
this is cron; on Render it is a Background Worker running
`scripts/render/run_jobs_worker.sh`, which calls the same bounded command in a
loop (20 s batches, 10 s pause). The job system itself is unchanged and needs
no Redis or broker.

| Setting | Value |
| --- | --- |
| Type | Background Worker (paid) |
| Repository / branch / region | Same as the web service |
| Build Command | `bash scripts/render/build.sh` |
| Start Command | `bash scripts/render/run_jobs_worker.sh` |
| Environment | Same variables as the web service |
| Instances | 1 (more are safe, jobs are claimed with `SKIP LOCKED`, but not needed) |

On deploy or restart Render sends SIGTERM; the loop finishes its current batch
and exits. If it is killed first, the job's lease expires (5 min) and the job
runs again (handlers are idempotent).

### What stops without the worker

| Feature | Job | Effect if no worker runs |
| --- | --- | --- |
| Registration, email confirmation, password reset | `notifications.deliver_email` | **No email is sent; new members cannot activate accounts.** Emails expire after their link lifetime |
| Memberships | `memberships.process_expiry` (15 min) | Expired/pending subscriptions are not processed |
| Videos | `videos.sync_status`, `videos.delete_media`, `videos.sweep_unfinished` | Uploaded videos stay "processing"; deleted media is not removed from Bunny |
| Advertising | `advertising.campaign_schedule` (1 min) | Scheduled campaigns never start or end |
| Verified viewing | `viewing.expire_sessions` (1 min) | Abandoned sessions are not finalized |
| Demo rewards | `rewards.evaluate_pending`, `rewards.reconcile_reversals` | Simulated reward decisions stay pending |
| Sandbox payments (only if enabled) | `payments.expire_intents`, `payments.poll_pending`, `payments.check_funding` | Intents never expire (payments are `disabled` by default) |
| Wallets | `wallets.reconcile` (1 h) | Wallet/ledger reconciliation does not run |
| Demo withdrawals | `withdrawals.reconcile_payouts`, `withdrawals.housekeeping` | Demo payouts are not reconciled |
| Maintenance | `core.*`, `notifications.prune_outbox` | No health heartbeat; old jobs, counters and emails are not pruned |

The administration console (`/manage/` → Operational health) shows a stale worker
heartbeat when the worker is not running.

**Alternative (not recommended):** a Render Cron Job running
`python manage.py run_jobs` every minute. It is billed by runtime, starts a
fresh container each minute, and gives up to a minute of email latency; the
worker is simpler and usually cheaper at this frequency.

### Monitoring

`python manage.py jobs_status` prints a JSON health report and exits 1 when the
heartbeat is stale, the backlog is old, leases have expired or the cache is
unavailable. On a paid instance run it from the shell; wiring it to an alert
(e.g. a Render Cron Job every 5 minutes) is a follow-up decision.

## 9. Email

Registration depends on email: accounts are activated from an emailed link.

* Choose a transactional email provider (any that offers SMTP on port 587 or
  465), verify the sending domain, and set the `DJANGO_EMAIL_*` variables (§4)
  on **both** the web service and the worker (the worker sends the email).
* Render blocks outbound SMTP from free instances. The worker is paid, so it
  can send; port 25 is blocked on all plans.
* With the default console backend, emails (including activation links) are
  printed to the worker's logs. Do not leave this on a public deployment.

## 10. Backups and database expiry

* **Free Render Postgres expires 30 days after creation**, then is deleted
  with all data after a further 14-day grace period unless upgraded. It has
  no backups and a 1 GB storage limit, and only one free database is allowed
  per workspace. Check `clipcash-db`'s plan and creation date in the dashboard.
* Paid databases include Render-managed recovery (point-in-time recovery and
  exports, depending on plan); confirm what the chosen plan provides.
* Independent of Render, take a logical backup before each migration deploy:
  `pg_dump --format=custom --no-owner "<External URL>" > clipcash-YYYYMMDD.dump`,
  stored encrypted and **outside the repository** (`*.dump` files must never
  be committed; they contain member data).

## 11. Automatic deploys from GitHub

* Keep Auto-Deploy **Off** until the first manual deploy is verified.
* Then use **On Commit** (deploys every push to `main`) or, if GitHub Actions
  CI is added later, **After CI Checks Pass**.
* Every deploy runs `check --deploy --fail-level ERROR` in the build, so a
  configuration that would enable sandbox payments or the demo video provider
  in production fails the build before anything changes.
* The repository is public: never push `.env`, database dumps or credentials.
  `.gitignore` already excludes `.env*` (except `.env.example`), keys, logs,
  `staticfiles/` and local databases.

## 12. Enabling HSTS (later)

After the site has run correctly over HTTPS on its final domain for some days:

1. Set `DJANGO_SECURE_HSTS_SECONDS=3600`, deploy, verify.
2. Increase to `31536000`. Only set `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=True`
   when every subdomain is HTTPS-only. Preload stays off (`SECURE_HSTS_PRELOAD = False`).

HSTS cannot be undone quickly: browsers remember it for the configured time.
Until then, `check --deploy` reports `security.W004`; that is expected.

## 13. Deployment blockers and open questions

| # | Item | Impact | Resolution |
| --- | --- | --- | --- |
| B1 | Background worker is paid-only | Without it no email is delivered, so nobody can activate an account; scheduled features stall (§8) | Approve a paid Background Worker |
| B2 | Email provider not chosen; free instances block SMTP | No activation, confirmation or reset emails | Choose a provider; set `DJANGO_EMAIL_*` |
| B3 | Client IP behind Render's proxy is unverified | With `DJANGO_TRUSTED_PROXY_COUNT=0`, every visitor shares Render's proxy address: per-IP rate limits (e.g. 10 registrations/hour, 50 failed logins/15 min) apply **site-wide**, and audit events record the proxy IP. Render does not document its `X-Forwarded-For` format; some reports say client-supplied values are passed through | After the first deploy, sign in from a known IP with count `1`, check the IP on the audit event in `/admin/` → Audit, and confirm a spoofed `X-Forwarded-For` header does not change it. If Render's format does not fit the existing "N trusted proxies" rule, reading `CF-Connecting-IP` would need a reviewed code change |
| B4 | `clipcash-db` plan unknown | Free: expires in 30 days, no backups | Confirm plan; upgrade before storing real member data |
| B5 | Bunny Stream credentials not configured | Video upload/playback fail (`videos.W001`); the videos feature flag is off by default | Configure when the video feature is to be enabled |
| B6 | Single database owner | Audit append-only trigger bypassable by the credential holder (§5) | Review role separation |
| B7 | Legal / provider approval for money | Real payments, rewards and payouts stay locked | Separate reviewed phase; not part of deployment |

## 14. First deployment checklist

1. Confirm `clipcash-db`'s region, plan and creation date.
2. Create an Environment Group with the §4 variables (paste the Internal URL; generate the secret key).
3. Create the Web Service (§6) with Auto-Deploy **Off**, linked to the group.
4. Paid: set the Pre-Deploy Command. Free: run §7.2 migrations from your machine first.
5. Deploy manually. Watch the build: `collectstatic` and `check --deploy` must succeed.
6. Open `https://<service>.onrender.com/healthz` → `ok`.
7. Open `http://<service>.onrender.com/` → redirected to HTTPS; pages load with styles.
8. Create the Super Administrator (§7.3); sign in at `/admin/`; confirm `/manage/` loads.
9. Create the Background Worker (§8); check `/manage/` → Operational health shows a fresh heartbeat.
10. Configure email (§9); register a test account; confirm the activation email arrives.
11. Resolve B3 (client IP) before announcing the site.
12. Confirm financial locks: `/manage/` → Operational health says "Payments are disabled here."
13. Turn Auto-Deploy on.

## 15. Rollback

* **Code:** Dashboard → service → Events/Deploys → choose the last good deploy
  → **Rollback**. Rollbacks redeploy the old code only; they do not undo
  migrations or environment variable changes.
* **Migrations:** never fake or roll back migrations on production. Prefer a
  forward fix. If a migration damaged data, restore from the backup taken
  before the deploy (§10) into a new database, verify it, then point
  `DATABASE_URL` at it.
* **Environment variables:** changing them triggers a redeploy; keep a private
  record of non-secret values so a bad change can be reverted.
* Deactivating Auto-Deploy stops further pushes from deploying while you investigate.

## 16. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Build fails at `check --deploy` with `accounts.E001` | `DJANGO_CACHE_URL` not set (per-process cache) | Set `dbcache://clipcash_cache` |
| Build fails with `payments.E002` / `videos.E002` | Sandbox payments or demo videos enabled | Set `DJANGO_PAYMENTS_MODE=disabled` / `VIDEO_PROVIDER=bunny` |
| Startup error `DATABASE_URL must be a PostgreSQL URL` | Wrong or malformed URL | Re-copy the Internal Database URL |
| Startup error mentioning `DJANGO_SECRET_KEY` or `DB_NAME` | `DJANGO_SECRET_KEY` or `DATABASE_URL` missing | Add the variable (and to the worker) |
| Deploy never becomes live; `/healthz` 503 | Database unreachable: wrong URL, different region, SSL mode | Check region and URL; see §5 on `sslmode` |
| `400 Bad Request` on every page | Host not allowed (custom domain) | Add it to `DJANGO_ALLOWED_HOSTS` |
| `403 CSRF verification failed` on forms | Origin not trusted, or proxy header missing | Add `https://<domain>` to `DJANGO_CSRF_TRUSTED_ORIGINS`; set `DJANGO_SECURE_PROXY_SSL_HEADER=True` |
| Redirect loop | `SECURE_SSL_REDIRECT` on without the proxy header | Set `DJANGO_SECURE_PROXY_SSL_HEADER=True` |
| Pages without styles / 500 on pages with static files | `collectstatic` missing or a template names a missing file | Check build log; run collectstatic locally with `DEBUG=False` |
| Server error `relation "clipcash_cache" does not exist` | Cache table not created | Run `predeploy.sh` (or `createcachetable`) |
| Emails never arrive | Worker not running, console backend, or SMTP blocked | §8, §9; check `/manage/` → Operational health and the outbox admin |
| Many users "rate limited" | Client IP is the proxy's (B3) | Resolve `DJANGO_TRUSTED_PROXY_COUNT` |
| Worker restarts repeatedly | Missing environment variables on the worker | Attach the same Environment Group |
| Memory errors on a 512 MB instance | Too many Gunicorn workers | Lower `WEB_CONCURRENCY` |
