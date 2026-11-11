# Public-production deployment runbook (NetPoa cPanel, `.co.tz`)

For the person who deploys ClipCash publicly on NetPoa (cPanel, Passenger, PostgreSQL, Bunny Stream). The
restricted-demo runbook ([netpoa_runbook.md](netpoa_runbook.md)) stays valid for demo deployments and is not
replaced. Environment template: [netpoa_production.env.example](netpoa_production.env.example).

**Not rehearsed on NetPoa.** Commands were checked against the repository (scripts, management commands,
`check --deploy` with the template's values), not on the host. Do the first run on a staging subdomain
(`staging.<domain>.co.tz`), then repeat for the real host.

## Ground rules

* **Three stages.** Stage 1 deploys safely with registration **closed** and payments **off**. Stage 2 opens
  registration after acceptance. Stage 3 enables manual Airtel Lipa payments only with the owner's **written
  authorization**. Each stage ends with a recorded acceptance.
* **Locked in every stage:** live gateway payments, sandbox payments, real-money rewards, withdrawals, advertiser
  settlement, complimentary demo memberships. Nothing here unlocks them.
* **Hosting capabilities:** the owner confirmed NetPoa supports the requirements in
  [netpoa_public_compatibility.md](netpoa_public_compatibility.md). The database scripts below still **prove** the
  critical ones (PostgreSQL 15+, `btree_gist`, owner/runtime separation) and stop on failure. Never work around a
  `STOP:` with a single all-powerful login.
* **Secrets:** never in a report, chat, commit, crontab line, `.htaccess`, the Setup Python App page or a log. Type
  them at `read -rs` or `psql -W` prompts. The database **owner** password is never stored on the server.
* Keep a private, offline record: release hash, dates, who did each step, non-secret settings.

## 0. Inputs

| Item | Notes |
|---|---|
| Release | The reviewed commit on local `main`, e.g. `b5be929` or later. **GitHub `origin/main` is behind (unpushed commits) — never deploy from GitHub unless it has been pushed to that exact hash.** |
| Domain | `<domain>.co.tz`, DNS access (registrar or Cloudflare) |
| cPanel | Account name (`<account>`), Terminal or SSH access |
| SMTP | Host, port 587 (STARTTLS) or 465 (TLS), user, password, verified sender on the domain, SPF/DKIM records |
| Bunny Stream | Library ID, API key, CDN host name, token security key; **Embed view token authentication on** |
| People | Two Super Administrators (different people); for Stage 3, named payment verifiers (two recommended) |
| Plans (Stage 3) | Owner-approved names, TZS prices, lengths in days |
| Airtel Lipa (Stage 3) | Business number and recipient name exactly as issued by Airtel |
| Your PC | Git, PostgreSQL 18 client tools (restore drill) |

## 1. Order of operations

| # | Step | § |
|---|---|---|
| 1 | Domain, DNS, TLS | 2 |
| 2 | Release archive from local Git, upload, unpack | 3 |
| 3 | Python application (Passenger) and dependencies | 4 |
| 4 | PostgreSQL: database, owner and runtime logins, privileges closed | 5 |
| 5 | `.env` (Stage 1 values) and `check --deploy` | 6 |
| 6 | Backup (existing database only) | 7 |
| 7 | Migrations as owner (creates `btree_gist`) | 8 |
| 8 | Runtime grants, separation and guard verification | 9 |
| 9 | `collectstatic`, Passenger restart, HTTPS checks | 10 |
| 10 | Cron and job-health monitoring | 11 |
| 11 | SMTP verification | 12 |
| 12 | Administrators, site configuration, Bunny verification | 13 |
| 13 | **Stage 1 acceptance** | 14 |
| 14 | First backup and restore drill | 15 |
| 15 | **Stage 2:** registration activation and acceptance | 16 |
| 16 | **Stage 3 (authorization required):** manual payments, controlled payment test, reconciliation | 17 |

## 2. Domain, DNS and TLS

1. cPanel → **Domains**: add `<domain>.co.tz` (and `staging.<domain>.co.tz` first). Document root: a new folder
   such as `~/clipcash_web` — **not** `public_html` of the existing site and not the application folder.
2. DNS: `A` (and `AAAA` if offered) records to the NetPoa server address shown in cPanel. With Cloudflare: proxied,
   SSL/TLS mode **Full (strict)** (never "Flexible"), "Always Use HTTPS" on.
3. TLS on the origin: cPanel → **SSL/TLS Status** → Run AutoSSL for the host (or install a Cloudflare Origin CA
   certificate under SSL/TLS → Manage SSL sites).
4. Email DNS for the sender domain: the SPF and DKIM records (and DMARC) the SMTP provider specifies.
5. Verify from your PC: `nslookup <domain>.co.tz` returns the expected address;
   `curl -sI https://<domain>.co.tz/` completes a TLS handshake (any HTTP status is fine before §10).

## 3. Release (from local Git)

On your PC, in the repository:

```powershell
git status                       # must be clean
git log --oneline -1             # write down <commit>; it must be the reviewed release
git archive --format=tar.gz -o clipcash-<commit>.tar.gz <commit>
Get-FileHash clipcash-<commit>.tar.gz -Algorithm SHA256   # record it
```

`git archive` contains only committed files: `.env`, `venv/`, dumps and local data never leave your PC. Upload with
cPanel File Manager or SFTP to `~/clipcash/releases/`, then in the cPanel Terminal:

```bash
sha256sum ~/clipcash/releases/clipcash-<commit>.tar.gz          # must match the recorded hash
mkdir -p ~/clipcash/releases/<commit> ~/clipcash/app ~/clipcash/logs ~/clipcash/backups
tar -xzf ~/clipcash/releases/clipcash-<commit>.tar.gz -C ~/clipcash/releases/<commit>
rsync -a --delete --exclude .env --exclude staticfiles/ --exclude tmp/ --exclude '*.log' --exclude 'media/*' \
      ~/clipcash/releases/<commit>/ ~/clipcash/app/
chmod 700 ~/clipcash ~/clipcash/logs ~/clipcash/backups
echo "<commit>" > ~/clipcash/app/RELEASE
```

(First release without `rsync`: `cp -a ~/clipcash/releases/<commit>/. ~/clipcash/app/`.)

## 4. Python application (Passenger)

cPanel → **Setup Python App** → **Create Application**:

| Field | Value |
|---|---|
| Python version | 3.13 |
| Application root | `clipcash/app` |
| Application URL | `<domain>.co.tz` (staging subdomain first) |
| Application startup file | `passenger_wsgi.py` |
| Application entry point | `application` |
| Environment variables | **none** (settings come from `~/clipcash/app/.env`; some hosts copy page variables into `.htaccess`) |

Then in the Terminal (the page shows the exact `source` line):

```bash
source /home/<account>/virtualenv/clipcash/app/3.13/bin/activate
cd ~/clipcash/app
python --version                     # 3.13.x
pip install --upgrade pip
pip install -r requirements/base.txt # Django 5.2.18, psycopg[binary] 3.3.6, whitenoise; gunicorn is unused here
python -c "import django, psycopg; print(django.get_version(), psycopg.__version__)"
which python                         # record: cron uses this path
```

## 5. PostgreSQL provisioning

cPanel → **PostgreSQL Databases**:

1. Create database `clipcash` → `<account>_clipcash`.
2. Create user `ccapp` → `<account>_ccapp` (the **runtime** login) with a generated password of letters and
   digits; add it to the database. The **owner** login is the cPanel account's own database login (`<account>`).

Inspect and close database-level privileges **as the owner** (read-only inspection first):

```bash
cd ~/clipcash/app
psql "host=localhost dbname=<account>_clipcash user=<account>" -W -f scripts/database/cpanel/00_inspect.sql
psql "host=localhost dbname=<account>_clipcash user=<account>" -W \
     -v runtime_role=<account>_ccapp -f scripts/database/cpanel/01_prepare_database.sql
```

`00_inspect.sql` shows the server version (needs **15+**), whether `btree_gist` is available and trusted, and the
owner. `01_prepare_database.sql` stops with `STOP:` if PostgreSQL is older than 15 or the logins cannot be
separated; it removes CREATE and TEMPORARY from the runtime login. Any `STOP:` → stop the deployment.

## 6. Environment (`~/clipcash/app/.env`)

```bash
cd ~/clipcash/app
umask 077
python -c "import secrets; print(secrets.token_urlsafe(64))"   # the new secret key
nano .env            # paste docs/deployment/netpoa_production.env.example, replace every <placeholder>
chmod 600 .env
python manage.py check --deploy --fail-level ERROR
```

Stage 1 values (full list and comments in the template):

| Variable | Stage 1 | Stage 2 | Stage 3 |
|---|---|---|---|
| `DJANGO_DEBUG` | `False` | | |
| `DJANGO_SECRET_KEY` | new 64-byte key | | |
| `DJANGO_ALLOWED_HOSTS` / `DJANGO_CSRF_TRUSTED_ORIGINS` | `<domain>.co.tz` / `https://<domain>.co.tz` | | |
| `DATABASE_URL` | **runtime** login URL | | |
| `DJANGO_CACHE_URL` | `dbcache://clipcash_cache` | | |
| `DJANGO_SECURE_SSL_REDIRECT` | `True` | | |
| `DJANGO_SECURE_PROXY_SSL_HEADER` | `False` (Passenger receives HTTPS) | | |
| `DJANGO_SECURE_HSTS_SECONDS` | `0` | | raise after a week of HTTPS (reviewed change) |
| `DJANGO_TRUSTED_PROXY_COUNT` / `_NETWORKS` | per proxy case (template) | | |
| `DJANGO_RESTRICTED_DEMO` | `False` | | |
| `DJANGO_PUBLIC_REGISTRATION_APPROVED` | `False` | **`True`** | |
| `DJANGO_PAYMENTS_MODE` | `disabled` | | |
| `DJANGO_MANUAL_MEMBERSHIP_PAYMENTS` | unset | unset | **`True`** (authorization required) |
| SMTP (`DJANGO_EMAIL_*`, `DJANGO_DEFAULT_FROM_EMAIL`) | provider values, TLS | | |
| Bunny (`VIDEO_PROVIDER=bunny`, `BUNNY_STREAM_*`) | Bunny values | | |

Expected `check --deploy` result at Stage 1: **no errors**; warnings `core.W003` (registration closed, intended)
and `security.W004` (HSTS 0) only. Any error → fix the value. (The template was run through this command with
fake values: unedited it fails `core.E003`/`accounts.E006`/`videos.E003`; filled in, Stages 1–3 pass.)

## 7. Backup (existing database only)

A brand-new database has nothing to back up. Before migrating an existing one, as the owner:

```bash
cd ~/clipcash/app
read -rs -p "Owner database URL: " BACKUP_DATABASE_URL; export BACKUP_DATABASE_URL; echo
bash scripts/database/backup.sh ~/clipcash/backups      # writes clipcash-<UTC>.dump + .sha256, validates it
unset BACKUP_DATABASE_URL
```

Owner URL form: `postgresql://<account>:<password>@localhost:5432/<account>_clipcash`. Download the `.dump` and
`.sha256`, store them encrypted off the server, then delete the server copy (`rm ~/clipcash/backups/clipcash-*`).

## 8. Migrations (owner login)

```bash
cd ~/clipcash/app
read -rs -p "Owner database URL: " DATABASE_URL; export DATABASE_URL; echo
python manage.py migrate --plan            # review the list
python manage.py migrate --noinput
python manage.py createcachetable
python manage.py showmigrations | grep -c "\[ \]"   # must print 0
unset DATABASE_URL
```

* `memberships.0001` runs `CREATE EXTENSION btree_gist` (a trusted extension; the owner may create it). If it fails
  with a permission error, ask NetPoa to run `CREATE EXTENSION btree_gist;` in `<account>_clipcash`, then re-run
  `migrate`. Do not edit or skip the migration.
* `core.0004` and `payments.0005` alter and create trigger functions: only the owner can run them.
* **Never reverse migrations in production.** Rollback is a restore (§19); reversing `payments.0004` would delete
  payment records.

## 9. Runtime grants and verification

```bash
cd ~/clipcash/app
# As the OWNER: data privileges for the runtime login; DELETE revoked wherever a delete guard exists.
psql "host=localhost dbname=<account>_clipcash user=<account>" -W \
     -v runtime_role=<account>_ccapp -f scripts/database/cpanel/02_grant_runtime.sql

# As the RUNTIME login: each must end with "passed".
psql "host=localhost dbname=<account>_clipcash user=<account>_ccapp" -W -f scripts/database/cpanel/03_verify_separation.sql
psql "host=localhost dbname=<account>_clipcash user=<account>_ccapp" -W -f scripts/database/roles/04_verify_runtime_role.sql
psql "host=localhost dbname=<account>_clipcash user=<account>_ccapp" -W -f scripts/database/verify_guards.sql
```

`verify_guards.sql` (read-only) checks by name the audit, ledger, wallet, withdrawal, advertising, membership and
manual-payment guard triggers, that **no** application trigger is disabled, the key integrity constraints,
`btree_gist`, pinned `search_path` and no SECURITY DEFINER functions; it ends with `guards verification passed` or
exits with an error. Re-run all of §9 after any change on cPanel's PostgreSQL Databases page (adding a user grants
it everything again) and after every deploy with migrations.

## 10. Static files, Passenger start, HTTPS

```bash
cd ~/clipcash/app
python manage.py collectstatic --noinput
mkdir -p tmp && touch tmp/restart.txt
curl -s https://<domain>.co.tz/healthz              # ok
curl -sI http://<domain>.co.tz/ | head -n 3        # 301/302 to https://
curl -sI https://<domain>.co.tz/accounts/login/ | grep -iE "set-cookie|content-security-policy|x-frame-options|strict-transport"
```

Cookies must carry `Secure` and `HttpOnly`; CSP and `X-Frame-Options: DENY` present. WhiteNoise serves
`/static/` with long cache headers; no web-served static folder exists. Application errors go to the Passenger log
named on the Setup Python App page (often `stderr.log` in the application root) — check it after the first start.

**Client address and proxy boundary:** sign in as a Super Administrator and open `/manage/health/` → Client
address. It must show your real public IP, also when you add a forged header
(`curl -H "X-Forwarded-For: 6.6.6.6" ...`). If it shows a Cloudflare or server address, fix
`DJANGO_TRUSTED_PROXY_COUNT`/`_NETWORKS` per the template's two cases, restart, and re-check. Details and the
optional origin lock: [netpoa_runbook.md §9](netpoa_runbook.md).

## 11. Cron and job-health monitoring

cPanel → **Cron Jobs**: set "Cron Email" to an address someone reads, then add exactly:

```cron
* * * * * /bin/bash /home/<account>/clipcash/app/scripts/netpoa/run_jobs_cron.sh /home/<account>/virtualenv/clipcash/app/3.13/bin/python
*/10 * * * * /bin/bash /home/<account>/clipcash/app/scripts/netpoa/jobs_health_cron.sh /home/<account>/virtualenv/clipcash/app/3.13/bin/python
```

* The first delivers email (activation, password reset, payment notices), expires memberships and runs schedules;
  bounded to 45 s, `flock` prevents overlap, output in `~/clipcash/logs/run_jobs.log` (mode 600, rotated at 5 MB).
* The second emails only when `jobs_status` reports a problem. Optional dead-man's switch: prefix it with
  `CLIPCASH_HEARTBEAT_URL=<uptime-service ping URL>`.
* Only a 5-minute cron allowed? Use `*/5` on the first line and `DJANGO_JOBS_HEARTBEAT_STALE_SECONDS=900`.

After five minutes: `python manage.py jobs_status` exits 0; `/manage/health/` shows Background jobs fresh.

## 12. SMTP verification

```bash
python manage.py email_diagnostics --connect                    # opens the SMTP connection (TLS, login if set); sends nothing
python manage.py email_diagnostics --send-to <your mailbox>     # queued; cron delivers within about a minute
```

The message must arrive in the inbox (not spam); in its headers SPF and DKIM pass. Registration must not open
(Stage 2) until this passes.

## 13. Administrators, site configuration, Bunny

1. `python manage.py createsuperuser` for two different people (strong unique passwords).
2. `/admin/` → **Site configuration**: time zone `Africa/Dar_es_Salaam`; support email and phone; videos **on**;
   memberships **on**; advertisers **off** unless separately approved; maintenance mode off. (Public registration
   stays closed by `DJANGO_PUBLIC_REGISTRATION_APPROVED=False`, whatever this page says.)
3. **Bunny:** `/manage/videos/` → upload one video you own (≥ 5 s); it reaches Ready and is published. Its embed URL
   **without** a token must be refused in a private window; if it plays, turn on Embed view token authentication in
   Bunny. `/manage/health/` shows Video provider healthy.
4. Staff permissions in `/admin/` per named person (recorded privately). Do **not** grant
   `payments.verify_manual_payments` before Stage 3.

## 14. Stage 1 acceptance (safe deployment)

| Check | Expected |
|---|---|
| `https://<domain>.co.tz/` | Home page; no "Restricted demonstration" label; "New accounts are not open to the public" note |
| `/accounts/register/` | **403** (registration closed) |
| Super Administrator sign-in, `/manage/`, `/admin/` | Work |
| `/manage/health/` | Database privilege separation **Healthy**; Background jobs fresh; Email delivery configured; Video provider healthy; Sandbox payments disabled |
| `/memberships/` | Plans list (if any) with "Online payment is not available yet" |
| `/payments/` and `/payments/membership/<any id>/pay/`, signed in | 404 (sandbox and manual payments off) |
| Wallet / withdrawals pages | No live money; withdrawals unavailable |
| `python manage.py check --deploy --fail-level ERROR` | Passes (`core.W003`, `security.W004` only) |
| `verify_guards.sql` as runtime login | `guards verification passed` |

## 15. First backup and restore drill

Backup as in §7 (now a populated database). Restore drill on your PC (local PostgreSQL 18):

```powershell
& "C:\Program Files\PostgreSQL\18\bin\createdb.exe" -U postgres clipcash_restore_<yyyymmdd>
```
```bash
read -rs -p "Restore URL (local): " RESTORE_DATABASE_URL; export RESTORE_DATABASE_URL; echo
bash scripts/database/restore_check.sh <file.dump>     # expect 13 of 13 lock triggers, 2 of 2 constraints
psql "$RESTORE_DATABASE_URL" -f scripts/database/verify_guards.sql   # expect: guards verification passed
unset RESTORE_DATABASE_URL
```

Compare the printed counts (accounts, audit events, journal entries) with the source, then drop the drill database.
From launch on: a backup daily and before every deploy with migrations. Ask NetPoa's backup schedule as a second
copy, not a replacement.

## 16. Stage 2 — registration activation

Preconditions: Stage 1 accepted and recorded; SMTP verified (§12); cron healthy; the owner approves opening
registration.

1. `.env`: `DJANGO_PUBLIC_REGISTRATION_APPROVED=True`; `python manage.py check --deploy --fail-level ERROR` (only
   `security.W004`); `touch tmp/restart.txt`.
2. `/admin/` → Site configuration → **public registration on**.
3. Acceptance with a mailbox you control:

| Check | Expected |
|---|---|
| `/accounts/register/` | 200 |
| Register | Activation email arrives within ~1 minute; link → set password → account active, email verified |
| Register the same address again | Same screen; an "already registered" email; no second account |
| Sign in, sign out, password reset by email | All work |
| Weak password at activation | Clear validation message |
| New member: dashboard, `/memberships/mine/`, a members-only video | No membership; playback refused |
| Choose a plan (if one exists) | Saved as pending; **no payment page** (Stage 3 not enabled); nothing activates |

**Rollback lever:** Site configuration → public registration off (immediate), or set the variable back to `False`
and restart.

## 17. Stage 3 — manual Airtel Lipa membership payments (written authorization required)

Preconditions, all required: the owner's **written authorization** for real payment collection; refund/reversal
procedure and verifier staffing decided ([PAY-01 doc §6, §11–12](../implementation/pay_01_manual_membership_payments.md));
real Lipa business number and recipient name from the owner; Stage 2 accepted; a fresh backup (§7).

1. **Plans:** `/manage/plans/` (Super Administrator) → create the owner-approved plans: name, description, TZS price,
   length in days, listed, entitlements. Nothing is pre-filled. Later price/length changes need no deploy; choices
   already made keep their quoted terms.
2. **Enable:** `.env` → `DJANGO_MANUAL_MEMBERSHIP_PAYMENTS=True` (keep `DJANGO_PAYMENTS_MODE=disabled`,
   `DJANGO_RESTRICTED_DEMO=False`); `check --deploy --fail-level ERROR` (no `payments.E00x`);
   `touch tmp/restart.txt`. Until details are published, members see "Payments are temporarily unavailable".
3. **Recipient:** `/manage/membership-payments/instructions/` → publish the business number and recipient name
   **exactly** as Airtel issued them, plus the payment steps; a second person checks them against the Airtel
   paperwork.
4. **Verifiers:** `/admin/` → users → give named staff `payments.verify_manual_payments` (view-only staff:
   `payments.view_manual_payments`). Record who and why.
5. **Controlled payment test** (owner-controlled member account, a cheap test plan created for this purpose):
   choose → pay the exact amount from the owner's phone → submit the Airtel reference → membership **not** active →
   a verifier finds the transaction in the official Airtel business portal/statement → approve with the re-typed
   amount and reference → membership active for the plan's length; payment and activation notices received. Submit
   a deliberately wrong reference once and reject it ("No matching transaction"). Deactivate the test plan.
6. **Reconciliation (same day):** Airtel statement total = approved claims on `/manage/membership-payments/?status=APPROVED`
   = ledger postings `membership.manual_payment_received` (CASH). If the test payment is refunded: manual refund
   from the Airtel account, cancel the test membership with reason **Refund**, and a Super Administrator reverses
   its ledger entry (PAY-01 doc §12).
7. Rewards and withdrawals stay disabled; `check --deploy` must still show no `core.E008`.

**Off switches:** retire the Lipa details in the console (stops new claims; pending ones stay reviewable), or remove
`DJANGO_MANUAL_MEMBERSHIP_PAYMENTS` and restart (payment pages 404; nothing is lost).

## 18. Later deploys

1. Backup (§7). 2. New release archive from local Git (§3) into `~/clipcash/releases/<new>`; keep the previous one.
3. `rsync` into `app/` (§3 command). 4. Virtualenv active: `pip install -r requirements/base.txt`.
5. If the release has migrations: §8 as owner, then all of §9. 6. `check --deploy --fail-level ERROR`,
`collectstatic --noinput`, `touch tmp/restart.txt`. 7. §14 checks (and §16/§17 checks for the stages already live).

## 19. Rollback

| Situation | Action |
|---|---|
| Bad code, no migration | `rsync` the previous release from `~/clipcash/releases/` into `app/`, `collectstatic`, `touch tmp/restart.txt` |
| Bad configuration | Restore the previous `.env` values (offline record of non-secret values), restart |
| Registration problem | Site configuration → public registration off, or `DJANGO_PUBLIC_REGISTRATION_APPROVED=False` + restart |
| Payment problem | Retire the Lipa details, or unset `DJANGO_MANUAL_MEMBERSHIP_PAYMENTS` + restart |
| Wrong plan price | Edit or deactivate the plan in `/manage/plans/`; affected payments follow the refund procedure |
| Serious incident | Site configuration → maintenance mode; or Setup Python App → **Stop**; remove the cron lines |
| Bad migration or data | Never reverse or fake migrations. Create a new empty database, restore the last verified dump as the owner (`pg_restore --no-owner --no-privileges -d <new db> <file.dump>`), run §5 (`01`), §9, point `DATABASE_URL` at it, restart |
| Credential exposed | Change it (cPanel / provider), update `.env`, restart, re-run §9, review the audit log |

## 20. Monitoring and logs

| Signal | Where |
|---|---|
| Site up | External uptime monitor on `https://<domain>.co.tz/healthz` (`ok`; also checks the database) |
| Background jobs | Health cron email, optional heartbeat service, `/manage/health/`, `python manage.py jobs_status` |
| Application errors | Passenger log from the Setup Python App page; cPanel → Errors; security events go to the same console log without emails, passwords or tokens |
| Job log | `~/clipcash/logs/run_jobs.log` |
| Payments (Stage 3) | `/manage/` attention list "Membership payments awaiting verification" (check daily), daily Airtel reconciliation |
| Email | Provider bounce/complaint reports |
| Resources | cPanel → Resource Usage (shared with the existing site) |

## 21. Known risks

All of [netpoa_runbook.md "Known risks"](netpoa_runbook.md) (access logs record each emailed link once, shared
resource limits, owner credential, no staff MFA), plus: open registration depends on correct client-address
settings for rate limits; registration depends on SMTP and cron; Stage 3 depends on staff checking the official
Airtel records, and refunds are manual; no external alerting beyond cron email and optional uptime/heartbeat
services.
