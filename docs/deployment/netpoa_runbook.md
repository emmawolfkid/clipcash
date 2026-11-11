# Restricted-demo deployment runbook (NetPoa cPanel)

For the person deploying ClipCash to NetPoa shared hosting (cPanel, Passenger, PostgreSQL). The Render guide
([render.md](render.md), [restricted_demo_runbook.md](restricted_demo_runbook.md)) stays valid for Render.

**Real payments, real rewards and real payouts stay locked. Nothing here enables them.**
Never paste a credential into a report, chat, commit, crontab line or log. Commands below read secrets with
`read -rs` or `psql -W` so they stay out of shell history.

**Do not start until [netpoa_compatibility.md](netpoa_compatibility.md) is complete**, with every row PASS or an
owner-approved decision. In particular: PostgreSQL 15+, `btree_gist`, and a passing database separation (D1–D4).

Not rehearsed yet: everything on NetPoa itself. Do the first run on a staging subdomain.

## 0. Before you start

| Item | Needed |
|---|---|
| Release | One reviewed commit on `main` (write down its hash) |
| Hostname | Its own subdomain, e.g. `demo.<your domain>`. Never the existing site's domain or folder |
| Cloudflare | DNS for that subdomain on Cloudflare, proxied (orange cloud) |
| Bunny Stream | Library ID, API key, CDN hostname, token key; **Embed view token authentication on**; one video you own or may use, at least 5 s long |
| Email | SMTP account (587 or 465) with a verified sender, or the no-email option in §8 |
| Demo people | Two Super Administrators (reward policies need two people), demo members, an advertiser owner |
| Your PC | PostgreSQL 18 client tools (for the restore drill) and Git |

## 1. Directory layout (outside `public_html`)

```
/home/<account>/
├── clipcash/
│   ├── app/                 ← Passenger application root: the release + .env (mode 600) + staticfiles/
│   ├── releases/<commit>/   ← unpacked releases, kept for rollback
│   ├── logs/                ← run_jobs.log (mode 600)
│   └── backups/             ← database dumps until you download them
├── virtualenv/…             ← created by "Setup Python App"
└── <subdomain document root>/  ← only the .htaccess that cPanel writes for Passenger
```

Code, `.env`, logs and backups never go in a web-served folder. Do not edit the existing site's folders,
`.htaccess` or Python application.

## 2. Order of operations

1. Compatibility checklist complete (above).
2. Release archive uploaded and unpacked (§3).
3. Python application and virtualenv created (§4).
4. `.env` written (§6), deploy check passes (§6).
5. Database: create, prepare, migrate as owner, grant runtime, verify (§5).
6. Static files collected, Passenger restarted (§7).
7. HTTPS and Cloudflare configured, smoke tests (§9, §13).
8. Super Administrators and demo content (§10).
9. Cron for jobs and health (§11), then email and Bunny checks (§8).
10. First backup and restore drill (§12).

## 3. Release

On your PC, from the repository, build an archive of exactly one commit. `git archive` contains only tracked files,
so `.env`, `venv/` and local data never leave your PC:

```powershell
git archive --format=tar.gz -o clipcash-<commit>.tar.gz <commit>
```

Upload it with cPanel File Manager (or SFTP) to `~/clipcash/releases/`, then in the Terminal:

```bash
mkdir -p ~/clipcash/releases/<commit> ~/clipcash/app ~/clipcash/logs ~/clipcash/backups
tar -xzf ~/clipcash/releases/clipcash-<commit>.tar.gz -C ~/clipcash/releases/<commit>
rsync -a --delete --exclude .env --exclude staticfiles/ --exclude tmp/ --exclude '*.log' --exclude 'media/*' \
      ~/clipcash/releases/<commit>/ ~/clipcash/app/
chmod 700 ~/clipcash ~/clipcash/logs ~/clipcash/backups
```

If `rsync` is missing, use an empty `app/` for the first release and `cp -a` instead.

## 4. Python application (Passenger)

cPanel → **Setup Python App** → **Create Application**:

| Field | Value |
|---|---|
| Python version | 3.13 (or the version approved in the checklist) |
| Application root | `clipcash/app` |
| Application URL | the ClipCash subdomain |
| Application startup file | `passenger_wsgi.py` (already in the release; keep it) |
| Application entry point | `application` |

Do **not** enter secrets as environment variables on this page: some hosts write them into the subdomain's
`.htaccess`. ClipCash reads its settings from `~/clipcash/app/.env` instead (§6), which the cron jobs also use.

The page shows the command to enter the virtualenv. In the Terminal:

```bash
source /home/<account>/virtualenv/clipcash/app/<version>/bin/activate
cd ~/clipcash/app
pip install -r requirements/base.txt
which python   # note this path: the cron lines need it
```

`gunicorn` installs too but is unused here; Passenger serves the app.

## 5. Database

Use the logins validated in checklist §3: **owner login** (the cPanel account's own database login) and **runtime
login** (a separate cPanel database user). The owner credential is typed only when needed and is never stored in
`.env` or cron.

1. cPanel → PostgreSQL Databases: create database `clipcash` (becomes `<account>_clipcash`) and user `ccapp`
   (becomes `<account>_ccapp`) with a generated password of letters and digits. Add the user to the database.
2. Close database-level privileges (owner login):

   ```bash
   cd ~/clipcash/app
   psql "host=localhost dbname=<account>_clipcash user=<account>" -W \
        -v runtime_role=<account>_ccapp -f scripts/database/cpanel/01_prepare_database.sql
   ```
   Any `STOP:` line means stop here (checklist §5). Do not work around it with a single login.
3. Migrate **as the owner login** (it must own every table and trigger function), after §6 so `.env` exists:

   ```bash
   read -rs -p "Owner database URL: " DATABASE_URL; export DATABASE_URL; echo
   python manage.py migrate --plan
   python manage.py migrate --noinput
   python manage.py createcachetable
   unset DATABASE_URL
   ```
   URL form: `postgresql://<account>:<password>@localhost:5432/<account>_clipcash`.
4. Grant the runtime login its data privileges (owner login):

   ```bash
   psql "host=localhost dbname=<account>_clipcash user=<account>" -W \
        -v runtime_role=<account>_ccapp -f scripts/database/cpanel/02_grant_runtime.sql
   ```
5. Verify **as the runtime login**. Both must end with "passed". Since RG-03, `04_verify_runtime_role.sql` also
   proves the runtime login cannot create temporary tables or schemas, unpin a guard function or switch to the owner
   role, and that migration `core.0004` has pinned every function's `search_path`:

   ```bash
   psql "host=localhost dbname=<account>_clipcash user=<account>_ccapp" -W -f scripts/database/cpanel/03_verify_separation.sql
   psql "host=localhost dbname=<account>_clipcash user=<account>_ccapp" -W -f scripts/database/roles/04_verify_runtime_role.sql
   ```

Re-run steps 2, 4 and 5 after any change on cPanel's PostgreSQL Databases page (adding a user to the database
grants it all database privileges again).

## 6. Environment (`~/clipcash/app/.env`)

```bash
cd ~/clipcash/app
umask 077
nano .env          # paste the values below, save
chmod 600 .env
python -c "import secrets; print(secrets.token_urlsafe(64))"   # the secret key; paste it, do not reuse it anywhere
```

| Variable | Value |
|---|---|
| `DJANGO_SECRET_KEY` | Newly generated; never the local one |
| `DJANGO_DEBUG` | `False` |
| `DJANGO_ALLOWED_HOSTS` | the ClipCash hostname |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://` + the ClipCash hostname |
| `DATABASE_URL` | `postgresql://<account>_ccapp:<password>@localhost:5432/<account>_clipcash` (**runtime** login) |
| `DJANGO_CACHE_URL` | `dbcache://clipcash_cache` |
| `DJANGO_SECURE_SSL_REDIRECT` | `True` |
| `DJANGO_SECURE_PROXY_SSL_HEADER` | `False` (Passenger receives HTTPS itself with Cloudflare "Full (strict)", §9) |
| `DJANGO_SECURE_HSTS_SECONDS` | `0` for now |
| `DJANGO_TRUSTED_PROXY_COUNT` | `1` (Cloudflare), then verify in §13 |
| `DJANGO_TRUSTED_PROXY_NETWORKS` | Cloudflare's current ranges from https://www.cloudflare.com/ips/ (IPv4 and IPv6, comma-separated). Forwarded headers are believed only from these peers (§9). Without it, `check --deploy` fails (`accounts.E007`) |
| `DJANGO_CLIENT_IP_HEADER` | Optional: `CF-Connecting-IP` instead of the proxy count. Only together with the networks above (`accounts.E005`) |
| `DJANGO_RESTRICTED_DEMO` | `True` (set explicitly) |
| `DJANGO_PAYMENTS_MODE` | `disabled` |
| `DJANGO_MEMBERSHIPS_COMPLIMENTARY_GRANTS` | `True` |
| `VIDEO_PROVIDER` | `bunny` |
| `BUNNY_STREAM_LIBRARY_ID`, `BUNNY_STREAM_API_KEY`, `BUNNY_STREAM_CDN_HOSTNAME`, `BUNNY_STREAM_TOKEN_SECURITY_KEY` | From Bunny |
| `DJANGO_EMAIL_BACKEND` | `django.core.mail.backends.smtp.EmailBackend` (or §8 no-email option) |
| `DJANGO_EMAIL_HOST`, `DJANGO_EMAIL_PORT`, `DJANGO_EMAIL_USE_TLS` or `DJANGO_EMAIL_USE_SSL`, `DJANGO_EMAIL_HOST_USER`, `DJANGO_EMAIL_HOST_PASSWORD`, `DJANGO_DEFAULT_FROM_EMAIL` | From the provider |
| `DJANGO_JOBS_HEARTBEAT_STALE_SECONDS` | Only if cron is every 5 minutes: `900` |

**Never set:** `DJANGO_DEBUG=True`, `DJANGO_RESTRICTED_DEMO=False`, `DJANGO_PUBLIC_REGISTRATION_APPROVED`,
`DJANGO_PAYMENTS_SANDBOX_ALLOWED`, `DJANGO_PAYMENTS_MODE=sandbox`, `VIDEO_DEMO_ALLOWED`,
`DJANGO_ADVERTISING_REPORT_MIN_COUNT` below 5, a console, locmem or file email backend (they keep activation and
reset links in logs or files), `DJANGO_PROXY_ORIGIN_RESTRICTED` (§9), or the owner database login anywhere in `.env`.

Then:

```bash
python manage.py check --deploy --fail-level ERROR
```

Expected warnings only: `security.W004` (HSTS 0); `notifications.W001` without SMTP; `videos.W001` without Bunny.
Any error: stop and fix the value. Since RG-03 these configurations are **errors**, so the command fails:
`DEBUG` on (`core.E002`); a missing, short or placeholder secret key (`core.E003`); `*` or empty allowed hosts
(`core.E004`); non-HTTPS trusted origins (`core.E005`); HTTPS redirect off (`core.E006`); any real-money feature enabled (`core.E008`); a console, locmem or
file email backend (`notifications.E001`); SMTP to a remote server without TLS/SSL (`notifications.E003`); forwarded
addresses trusted without a proxy network list (`accounts.E005`, `accounts.E007`); malformed Bunny values
(`videos.E003`, `videos.E004`). The command checks configuration only; it cannot see the hosting facts in §9 and §13.

## 7. Static files and start

```bash
cd ~/clipcash/app
python manage.py collectstatic --noinput
mkdir -p tmp && touch tmp/restart.txt      # Passenger restarts the app on the next request
```

WhiteNoise serves the collected files through Passenger; no web-served static folder is needed.

## 8. Email and Bunny

* **SMTP:** `python manage.py email_diagnostics --connect`, then, once cron runs (§11),
  `python manage.py email_diagnostics --send-to <your address>`; the message arrives within about a minute.
* **No SMTP yet:** `DJANGO_EMAIL_BACKEND=django.core.mail.backends.dummy.EmailBackend`,
  `DJANGO_DEMO_EMAIL_VERIFICATION_OPTIONAL=True`, `DJANGO_DEMO_ACCOUNTS=<comma-separated demo emails>`. Nobody can
  reset a password by email in this mode; staff reset passwords in `/admin/`.
* **Bunny:** after publishing a video, open its embed URL **without** a token in a private window. It must be
  refused. If it plays, turn on Embed view token authentication in Bunny before any demo.

## 9. HTTPS, Cloudflare and the origin

1. cPanel → **SSL/TLS Status**: run AutoSSL for the subdomain (or install a Cloudflare Origin CA certificate under
   SSL/TLS → Manage SSL sites).
2. Cloudflare → SSL/TLS: **Full (strict)**. Never "Flexible": the origin would receive HTTP and the HTTPS redirect
   would loop. Keep "Always Use HTTPS" on.
3. **Client addresses and the proxy boundary (RG-01 S1, fixed in code by RG-03).** ClipCash believes
   `X-Forwarded-For` (or the configured `CF-Connecting-IP`) only when the connection itself comes from a network in
   `DJANGO_TRUSTED_PROXY_NETWORKS`. A request sent straight to the server is attributed to its own address, whatever
   headers it carries, so forged headers cannot evade per-address limits or plant audit addresses. `X-Real-IP`,
   `CF-Connecting-IP` and `True-Client-IP` are never read unless configured. This holds only if Passenger sees the
   real peer address. Check which case applies (checklist §2.6) and verify it in §13:
   * **The web server passes Cloudflare's address as the peer** (usual for Apache without mod_remoteip): keep the
     settings in §6. On `/manage/health/` through Cloudflare, "Connection from a trusted proxy network" must say
     **Yes** and the resolved address must be yours.
   * **The web server already replaces the peer with the visitor's address** (mod_remoteip, LiteSpeed "use client
     IP in header"): set `DJANGO_TRUSTED_PROXY_COUNT=0`, leave the networks and header unset, and confirm on
     `/manage/health/` that the connection address is already your own. The web server's own trust list then decides
     who may set the address; ask NetPoa that it lists only Cloudflare's ranges.
   * Optional extra layer: limit the subdomain to Cloudflare in its `.htaccess` (below cPanel's Passenger block), only
     in the first case:

     ```apache
     <RequireAny>
         # Paste the current ranges from https://www.cloudflare.com/ips/ (IPv4 and IPv6), one per line:
         Require ip <cloudflare range>
     </RequireAny>
     ```
     Test from your PC: `curl -sk -o /dev/null -w "%{http_code}" --resolve <host>:443:<server IP> https://<host>/healthz`
     must print `403`, while `https://<host>/healthz` through Cloudflare prints `200`. With this in place (and only
     then) `DJANGO_PROXY_ORIGIN_RESTRICTED=True` may replace the network list; the list is still preferred.
   * Cloudflare's ranges change rarely; re-check https://www.cloudflare.com/ips/ before each deploy.
4. HSTS stays `0` until HTTPS has worked for a week; then raise it in a separate reviewed change.

## 10. Accounts and demo content

As in [restricted_demo_runbook.md](restricted_demo_runbook.md) §5 steps 7–11, run from `~/clipcash/app` with the
virtualenv active (the runtime login in `.env` is enough):

1. `python manage.py createsuperuser` twice (two different people).
2. `/admin/` → Site configuration: memberships, videos and advertisers on; time zone `Africa/Dar_es_Salaam`;
   support contacts.
3. Demo content: plan, category, Bunny video (published), advertiser, campaign, reward policy (second Super
   Administrator approves), complimentary memberships from `/manage/members/`.
4. Staff permissions per person in `/admin/`, recorded privately. Never run `provision_staff_permissions --apply`
   with the runtime login.
5. `python manage.py demo_readiness`: no `FAIL`.

## 11. Background jobs (cron)

Background jobs deliver email, start scheduled campaigns, evaluate demo rewards and expire sessions. Without them,
no email is sent.

cPanel → **Cron Jobs**. Set "Cron Email" to an address you read. Add exactly these two lines (paths from §4):

```cron
* * * * * /bin/bash /home/<account>/clipcash/app/scripts/netpoa/run_jobs_cron.sh /home/<account>/virtualenv/clipcash/app/<version>/bin/python
*/10 * * * * /bin/bash /home/<account>/clipcash/app/scripts/netpoa/jobs_health_cron.sh /home/<account>/virtualenv/clipcash/app/<version>/bin/python
```

| Property | How it is handled |
|---|---|
| Secrets | None on the cron line; both scripts read `~/clipcash/app/.env` through Django |
| Overlap | `flock -n` skips a minute while the previous run is busy; without `flock`, overlap is still safe (SKIP LOCKED, leases) |
| Duration | `run_jobs --max-seconds 45`; at most 100 jobs per run (about 1.4 s for 100 emails locally) |
| Duplicate email | At-least-once: only a run killed between the SMTP server accepting a message and the success being recorded can resend it |
| Noise | `run_jobs` output goes to `~/clipcash/logs/run_jobs.log` (mode 600, rotated at 5 MB); the health line emails only on a problem |
| Cron itself stopping | Not detectable from inside cron. Optional: set `CLIPCASH_HEARTBEAT_URL=<check URL of an uptime service>` in front of the health command; that service alerts when pings stop |
| 5-minute cron only | Use `*/5` for the first line and set `DJANGO_JOBS_HEARTBEAT_STALE_SECONDS=900` |

Check after five minutes: `/manage/health/` shows Background jobs fresh, and
`python manage.py jobs_status` exits 0.

## 12. Backups and restore drill

Backups need the owner login (it can read everything). Run before every deploy with migrations, and at least daily
during the demo:

```bash
cd ~/clipcash/app
read -rs -p "Owner database URL: " BACKUP_DATABASE_URL; export BACKUP_DATABASE_URL; echo
bash scripts/database/backup.sh ~/clipcash/backups
unset BACKUP_DATABASE_URL
```

Download the `.dump` and `.sha256` to your PC (File Manager or SFTP), store them encrypted, then delete the server
copy. Dumps contain member data.

**Restore drill (on your PC, once before the demo).** cPanel database names carry an account prefix, so the drill
uses your local PostgreSQL 18, where the safety check in `restore_check.sh` works:

```powershell
& "C:\Program Files\PostgreSQL\18\bin\createdb.exe" -U postgres clipcash_restore_<date>
```
```bash
read -rs -p "Restore URL (local): " RESTORE_DATABASE_URL; export RESTORE_DATABASE_URL; echo
bash scripts/database/restore_check.sh <file.dump>
unset RESTORE_DATABASE_URL
```
Expect `lock triggers present: 13 of 13` and `lock constraints present: 2 of 2`. Drop the drill database afterwards.

Also ask NetPoa to confirm that cPanel account backups include PostgreSQL (checklist §4); they are a second copy,
not a replacement.

## 13. Smoke tests

| Check | Expected |
|---|---|
| `curl -I http://<host>/` | Redirect to `https://` |
| `curl https://<host>/healthz` | `ok` |
| `curl -s -o /dev/null -w "%{http_code}" https://<host>/accounts/register/` | `403` (registration closed) |
| Headers on `/accounts/login/` | `Secure` cookies, `Content-Security-Policy`, `X-Frame-Options: DENY` |
| `/manage/health/` (Super Administrator) | Database privilege separation **Healthy**; Background jobs fresh; "Payments are disabled here" |
| Client address on `/manage/health/` | Your real public IP. Repeat with `curl -H "X-Forwarded-For: 6.6.6.6"` through Cloudflare: the address shown is still yours. A Cloudflare address means the proxy count is wrong |
| Direct origin, bypassing Cloudflare | `curl -sk --resolve <host>:443:<server IP> -H "X-Forwarded-For: 6.6.6.6" https://<host>/healthz` answers (or `403` with the optional `.htaccess` lock); then sign in through Cloudflare and check `/manage/health/` still shows your own address, never `6.6.6.6` |
| Email change | Request a change, open the link in a private window: it asks you to sign in, then (after 5 minutes) for your password; afterwards your other browser is signed out |
| Bunny embed without token | Refused |
| Role checks | As in [restricted_demo_runbook.md](restricted_demo_runbook.md) §6 "By role" |

## 14. Later deploys

1. Back up (§12).
2. Upload and unpack the new release to `~/clipcash/releases/<new commit>` (§3), keep the previous one.
3. `rsync -a --delete --exclude .env --exclude staticfiles/ --exclude tmp/ --exclude '*.log' --exclude 'media/*' ~/clipcash/releases/<new>/ ~/clipcash/app/`
4. `pip install -r requirements/base.txt` (virtualenv active).
5. If the release has migrations: run them **as the owner login** (§5 step 3), then §5 steps 4–5. Migrations must
   be additive for the running app to keep working; check `migrate --plan` first.
6. `python manage.py check --deploy --fail-level ERROR`, `collectstatic --noinput`, `touch tmp/restart.txt`.
7. Smoke tests (§13).

## 15. Rollback

| Situation | Action |
|---|---|
| Bad code, no migration | rsync the previous release from `~/clipcash/releases/` into `app/` (§14 step 3), `collectstatic`, `touch tmp/restart.txt` |
| Bad configuration | Restore the previous `.env` values (keep a private, offline record of non-secret values), `touch tmp/restart.txt` |
| Serious demo problem | `/admin/` → Site configuration → maintenance mode; or in Setup Python App, **Stop** the application. Remove the cron lines to stop jobs |
| Bad migration or data | Never fake or reverse migrations. Create a new empty database, restore the last dump into it as the owner login (`pg_restore --no-owner --no-privileges`), run §5 steps 2, 4 and 5 on it, and point `DATABASE_URL` at it |
| Credential exposed | Change the password in cPanel → PostgreSQL Databases (or the provider), update `.env`, `touch tmp/restart.txt`, re-run §5 steps 2, 4, 5, review audit logs |
| Role verification fails after a cPanel change | Do **not** switch the app to the owner login. Re-run §5 steps 2, 4 and 5 |

## 16. Monitoring

| Signal | Where |
|---|---|
| Site up | An external uptime monitor on `https://<host>/healthz` (expects `ok`, checks the database) |
| Jobs healthy | Health cron email (§11), optional heartbeat service, `/manage/health/` |
| Application errors | The Passenger log file named on the Setup Python App page (often `stderr.log` in the application root), and cPanel → Errors |
| Job log | `~/clipcash/logs/run_jobs.log` |
| Resources | cPanel → Resource Usage: CPU and memory faults show limits being hit. Both sites share one set of limits |

## 17. Restricted-demo safeguards (check each time)

* `DJANGO_RESTRICTED_DEMO=True`: registration answers 403. The deploy check does **not** warn if this is set to
  `False`, so check the smoke test.
* `DJANGO_PAYMENTS_MODE=disabled`; `/manage/health/` says payments are disabled. Real money is locked in code and by
  database triggers in every mode.
* The runtime login passes both verification scripts; `/manage/health/` shows privilege separation Healthy.
* No console email backend; no secrets in cron lines, `.htaccess`, tickets or chats.

## Known risks on this host

* **Access logs (cannot be fixed in code).** Apache (and Cloudflare) record the URL of every request, and the
  application cannot switch that off. Each emailed link reaches the logs once, on its first request: activation,
  password-reset and (since RG-03) email-change links then move the token into the session and redirect to a
  token-free URL; email-confirmation links keep it for the confirming POST. Token pages send no `Referer`
  (`Referrer-Policy: no-referrer`) and are not cached, and the application's own logs redact all four link types.
  Tokens are single-use and expire (1 h for resets, 24 h for verification and email changes); an email-change link
  is useless without the owner's signed-in session and password. Keep cPanel access to the owner, and turn off
  "Archive logs" in cPanel → Raw Access if you do not need them.
* **Trigger functions and search_path (fixed by RG-03).** Migration `core.0004` pins every ClipCash database
  function to `search_path = pg_catalog, public, pg_temp`, so a session's own temporary tables or schemas can no
  longer redirect a protected check. The cPanel scripts still deny the runtime login CREATE and TEMPORARY as a second
  layer; do not grant them back. `roles/04_verify_runtime_role.sql` fails if any function is unpinned.
* **Shared limits.** The existing site and ClipCash share CPU, memory and process limits; each sign-in costs about
  a second of CPU.
* **Owner credential.** The cPanel account's database login may also own the existing site's database. Type it only
  when needed; never store it on the server.
* **No staff MFA.** Use strong unique passwords and few staff accounts.
