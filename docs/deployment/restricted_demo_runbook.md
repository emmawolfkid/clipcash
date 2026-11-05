# Restricted-demo deployment runbook (Render)

For the person performing the deployment. Background and troubleshooting: [render.md](render.md).

**Real payments, real rewards and real payouts stay locked. Nothing here enables them.**
Never paste a credential into a report, chat, commit or log. Prompts below read secrets so they stay
out of shell history; close the terminal afterwards.

Rehearsed on 5 November 2026 from commit `175d413` on a throwaway PostgreSQL 18 server: fresh migrations
as a non-superuser owner, role split, runtime role, backup and restore. Not rehearsed: Render itself,
Gunicorn on Linux, Bunny Stream, SMTP (see "What the rehearsal could not prove").

## 0. Confirm before starting

| Item | Where | Needed |
|---|---|---|
| Web service plan | Render | Paid recommended (no spin-down, shell, SMTP) |
| Background worker | Render | **Paid worker required.** Without it campaigns, rewards, expiries and email stop |
| `clipcash-db` plan, region, creation date | Render | Free plan expires after 30 days with no backups. Same region as the services |
| Bunny Stream library | Bunny | Library ID, API key, CDN hostname, token key; **Embed view token authentication on** |
| Demo content | Bunny | At least one video you own or are licensed to use, at least 5 seconds long |
| Email | Provider | SMTP on 587 or 465 with a verified sender, or the no-email option in §3 |
| Demo account list | You | Two Super Administrators (reward policies need two people), demo members, an advertiser owner |
| Commits pushed | GitHub | Render deploys from `main` on GitHub. Decide what is included before pushing |

## 1. Back up (skip only if `clipcash-db` is empty)

PowerShell for the prompts, Git Bash for the `.sh` scripts. `psql` and `pg_dump` must be on PATH (`C:\Program Files\PostgreSQL\18\bin`). Use the **External** URL from your own machine,
and restrict the database's inbound IP allow-list to your address while you work.

```bash
read -rs -p "External URL (default user): " BACKUP_DATABASE_URL; export BACKUP_DATABASE_URL; echo
bash scripts/database/backup.sh "<directory outside the repository>"
unset BACKUP_DATABASE_URL
```

Do one restore drill before relying on backups: create an empty database named `clipcash_restore_<date>`, then

```bash
read -rs -p "Restore URL: " RESTORE_DATABASE_URL; export RESTORE_DATABASE_URL; echo
bash scripts/database/restore_check.sh "<file.dump>"
unset RESTORE_DATABASE_URL
```

Expect `lock triggers present: 13 of 13` and `lock constraints present: 2 of 2`. Compare the counts with the source.

## 2. Database: migrate, then separate roles (CC-005)

From a clean checkout of the commit being deployed, in PowerShell:

```powershell
$env:DATABASE_URL = Read-Host "External URL of clipcash-db (default user)"
$env:DJANGO_DB_SSLMODE = "require"; $env:DJANGO_DEBUG = "False"; $env:DJANGO_CACHE_URL = "dbcache://clipcash_cache"
psql $env:DATABASE_URL -Atc "SELECT version(); SELECT rolsuper, rolcreaterole FROM pg_roles WHERE rolname = current_user;"
python manage.py migrate --plan
python manage.py migrate --noinput
python manage.py createcachetable
```

`rolcreaterole` must be `t`. **If it is `f`, stop:** ask Render support; do not run the application as the owner
without a decision (see "Remaining risks").

Separate the roles, using generated passwords with letters and digits only so they are safe in URLs:

```powershell
psql $env:DATABASE_URL -v app_db=<database name> -f scripts/database/roles/01_create_roles.sql
psql $env:DATABASE_URL -c "\password clipcash_migrator"
psql $env:DATABASE_URL -c "\password clipcash_app"
psql $env:DATABASE_URL -f scripts/database/roles/02_transfer_ownership.sql
$env:DATABASE_URL = Read-Host "External URL with clipcash_migrator"
psql $env:DATABASE_URL -f scripts/database/roles/03_grant_runtime.sql
$env:DATABASE_URL = Read-Host "External URL with clipcash_app"
psql $env:DATABASE_URL -f scripts/database/roles/04_verify_runtime_role.sql
Remove-Item Env:DATABASE_URL
```

The notice `permission will be denied to set role` from step 01 is expected. Step 02 must end with both counts at 0,
and step 04 must end with **`Runtime role verification passed`**. Otherwise stop.

Step 02 runs once; a second run stops with an error and changes nothing. Only step 03 is ever repeated.

Build the two URLs from the Render URL by replacing only the user and password:
`postgresql://clipcash_app:<password>@<host>/<database>`. These roles are not listed in Render's dashboard.

## 3. Environment (one Environment Group, attached to web and worker)

| Variable | Value |
|---|---|
| `DATABASE_URL` | **Internal** URL with **`clipcash_app`** |
| `DJANGO_SECRET_KEY` | Render "Generate"; never reuse the local key |
| `DJANGO_DEBUG` | `False` |
| `DJANGO_DB_SSLMODE` | `require` |
| `DJANGO_CACHE_URL` | `dbcache://clipcash_cache` |
| `DJANGO_SECURE_PROXY_SSL_HEADER` | `True` |
| `DJANGO_SECURE_SSL_REDIRECT` | `True` |
| `DJANGO_SECURE_HSTS_SECONDS` | `0` for now (render.md §12) |
| `DJANGO_TRUSTED_PROXY_COUNT` | `2`, then verify in §6 |
| `DJANGO_RESTRICTED_DEMO` | `True` |
| `DJANGO_PAYMENTS_MODE` | `disabled` |
| `DJANGO_MEMBERSHIPS_COMPLIMENTARY_GRANTS` | `True` (how demo members get access without payment) |
| `VIDEO_PROVIDER` | `bunny` |
| `BUNNY_STREAM_LIBRARY_ID`, `BUNNY_STREAM_API_KEY`, `BUNNY_STREAM_CDN_HOSTNAME`, `BUNNY_STREAM_TOKEN_SECURITY_KEY` | From Bunny |
| `DJANGO_DEFAULT_FROM_EMAIL` | A sender your provider verified |
| `DJANGO_EMAIL_BACKEND` | `django.core.mail.backends.smtp.EmailBackend` |
| `DJANGO_EMAIL_HOST`, `DJANGO_EMAIL_PORT`, `DJANGO_EMAIL_USE_TLS`, `DJANGO_EMAIL_HOST_USER`, `DJANGO_EMAIL_HOST_PASSWORD` | From the provider |
| `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` | Only with a custom domain |

**No SMTP yet?** Use `django.core.mail.backends.dummy.EmailBackend`, plus
`DJANGO_DEMO_EMAIL_VERIFICATION_OPTIONAL=True` and `DJANGO_DEMO_ACCOUNTS=<comma-separated demo emails>`. Do not leave the
default console backend: it prints activation and password-reset links into the logs.

**Never set:** `DJANGO_DEBUG=True`, `DJANGO_PAYMENTS_SANDBOX_ALLOWED`, `VIDEO_DEMO_ALLOWED`,
`DJANGO_ADVERTISING_REPORT_MIN_COUNT` below 5 (the default is 5; leave it unset),
`MIGRATION_DATABASE_URL` (keeping the migrator credential off Render; see §7).

## 4. Services

Create both with Auto-Deploy **Off**, same region as the database, branch `main`.

| Setting | Web Service | Background Worker |
|---|---|---|
| Build Command | `bash scripts/render/build.sh` | `bash scripts/render/build.sh` |
| Pre-Deploy Command | **Empty** (§7) | n/a |
| Start Command | `gunicorn --config config/gunicorn.conf.py config.wsgi:application` | `bash scripts/render/run_jobs_worker.sh` |
| Health Check Path | `/healthz` | n/a |

`build.sh` runs `collectstatic` and `check --deploy --fail-level ERROR`, so a build fails if sandbox payments, the demo
video provider, a proxy count of 0 or a missing shared cache is configured.

Expected build warning: `security.W004` (HSTS, deliberately 0). `notifications.W001` appears only without SMTP, and
`videos.W001` only without Bunny.

## 5. Order of actions

1. Back up (§1). 2. Migrate and separate roles (§2). 3. Create the Environment Group (§3).
4. Create and manually deploy the Web Service; watch `collectstatic` and `check --deploy`.
5. Open `https://<service>.onrender.com/healthz`: expect `ok`.
6. Create and deploy the Background Worker.
7. In a Render shell (paid) or locally with the `clipcash_app` External URL, create the two Super Administrators:
   `python manage.py createsuperuser`
8. Sign in at `/admin/`. In **Site configuration**: turn on memberships, videos and advertisers; check the time zone
   (`Africa/Dar_es_Salaam`) and support contacts.
9. Prepare demo content: plan, category, Bunny video (published), advertiser, campaign, reward policy (a second
   Super Administrator approves), complimentary memberships from `/manage/members/`.
10. Grant staff permissions (below). 11. Run `python manage.py demo_readiness`: expect no `FAIL`.
12. Run the smoke tests (§6). Leave Auto-Deploy off during the demo.

**Staff permissions.** Super Administrators hold everything. Nobody else gets Support or Reports automatically:
Content Managers hold only video permissions. Grant only what a named person needs, per user, in `/admin/`:
`support.view_tickets`, `support.manage_tickets`, `reports.view_<area>_report`, `reports.export_reports`.
Record who holds what. Do not run `provision_staff_permissions --apply` with the runtime credential.

## 6. Smoke tests

**Platform**

| Check | Expected |
|---|---|
| `curl -I http://<host>/` | 301 to `https://` |
| `curl https://<host>/healthz` | `ok` |
| `curl -s -o /dev/null -w "%{http_code}" https://<host>/accounts/register/` | `403` (registration closed) |
| Response headers on `/accounts/login/` | `Secure` on cookies, `Content-Security-Policy`, `X-Frame-Options: DENY` |
| `/manage/health/` as Super Administrator | Database privilege separation **Healthy**; Background jobs fresh; "Payments are disabled here" |
| Client address on `/manage/health/` | Your real public IP. Repeat with `curl -H "X-Forwarded-For: 6.6.6.6"`: unchanged. A Cloudflare address means the proxy count is wrong |
| Bunny embed URL without a token, in a private window | **Refused.** If it plays, token authentication is off: fix this first |

**By role**

| Role | Steps | Expected |
|---|---|---|
| Visitor | Homepage, video library, plans, sign in | Styled pages; demo notice; no registration |
| Member | Dashboard, video library, play a video by keyboard and mouse, sponsored video to verified result | Video plays; verified view; simulated reward labelled, wallet stays 0 |
| Member | Open a Support ticket, then reply | Reference issued; visible only to that member |
| Advertiser owner | Campaign page, Reports tab | Own organization only; small counts read "fewer than 5"; no export button |
| Other advertiser | Open the first advertiser's URL | 404 |
| Support agent | Queue, assign, internal note, public reply | Customer sees the reply, never the note |
| Staff with one report permission | Reports, Analytics, export a CSV | Only permitted sections; export recorded in audit logs |
| Content Manager | `/manage/` | Video console only; no Reports, Analytics or Support queue |
| Super Administrator | Overview, Reports, Analytics, audit logs | Labels separate: real (locked, 0), sandbox, simulated, demo, demo budget (not money) |
| Email (if SMTP) | `python manage.py email_diagnostics --connect`, then `--send-to <you>` | Connects; message arrives through the worker |

## 7. Later deploys and migrations

The Pre-Deploy Command stays empty so the migrator credential never sits in Render's environment. Before deploying code
with new migrations, from your machine: back up, then `DATABASE_URL=<clipcash_migrator External URL>`,
`python manage.py migrate --plan`, `migrate --noinput`, `createcachetable`, and rerun `03_grant_runtime.sql`. Remove the
environment variable, then deploy. A running application stays compatible only if migrations are additive; check first.

## 8. Rollback

| Situation | Action |
|---|---|
| Bad code | Render → service → Deploys → last good deploy → **Rollback**. Do the same for the worker |
| Bad configuration | Revert the variable (this redeploys). Keep a private record of non-secret values |
| Serious demo problem | Admin → Site configuration → maintenance mode, or suspend the services |
| Bad migration or data | Never fake or reverse migrations. Restore the backup into a new database, run `restore_check.sh`, apply `01`–`04`, point `DATABASE_URL` at it |
| Credential exposed | Change it with `\password` (or Render for the default user), update the group, redeploy, review audit logs |
| Role split causes errors | Do **not** switch the app to the owner login. Read the error, rerun `03`, then `04` |

## What the rehearsal could not prove

Check these first on Render: whether the default user has CREATEROLE on this database; Gunicorn on Linux (the rehearsal
used Django's server on Windows); TLS to the database; the real proxy chain; Bunny playback, tokens and player keyboard
controls; SMTP delivery.

## Remaining risks

* The default Render user still owns the power to change roles. Keep its URL off every service and store it privately.
* Staff multi-factor authentication does not exist yet; use strong unique passwords and few staff accounts.
* The privacy threshold limits direct disclosure only. Comparing totals or date ranges can narrow down a hidden count.
* Audit logs record IP addresses and user agents; tell demo participants as your privacy terms require.
* The Swahili translation has not had a human review.
