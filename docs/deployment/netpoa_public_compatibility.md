# NetPoa public-production compatibility checklist (read-only)

For the owner and whoever deploys ClipCash publicly on NetPoa (cPanel, Passenger, PostgreSQL) under a `.co.tz`
domain. It extends [netpoa_compatibility.md](netpoa_compatibility.md) (the restricted-demo checklist, whose probe
procedures are reused here) with what a **public launch with real registration and manual Airtel Lipa membership
payments** additionally needs.

**Nothing in this document changes the server.** Every probe is read-only except the throwaway-database test in
[netpoa_compatibility.md §3](netpoa_compatibility.md), which creates and deletes a probe database.

**Status at writing (NET-01, baseline `b22ca84`):** only two hosting facts are known: cPanel offers "PostgreSQL
Databases", and NetPoa support confirmed outbound HTTPS (port 443). **Every other hosting row is UNVERIFIED.** Do not
start [netpoa_public_runbook.md](netpoa_public_runbook.md) until each row is PASS or carries a written owner decision.

## Part 1 — Application capabilities (verified in the repository)

These are proven by the automated PostgreSQL suite (1,480 tests at `b22ca84`) and `manage.py check --deploy` run in
a subprocess with production-like settings. They say nothing about NetPoa.

| # | Capability | Evidence |
|---|---|---|
| A1 | Runs under Passenger: `passenger_wsgi.py` exposes `application`, imports `config.wsgi`, no host-specific paths | `passenger_wsgi.py`; `apps/core/tests/test_netpoa_deployment.py` |
| A2 | Python 3.13.14 + Django 5.2.18 + psycopg 3.3.6 (binary) | `requirements/base.txt`; local test runs |
| A3 | PostgreSQL only; tests refuse any other engine; database guards (triggers, constraints, pinned `search_path`) | `config/test_runner.py`; `apps/core/tests/test_database_hardening.py` |
| A4 | Owner/runtime login separation scripts and verification | `scripts/database/cpanel/00–03`, `scripts/database/roles/04_verify_runtime_role.sql`; `apps/administration/tests/test_database_roles.py` |
| A5 | No long-running worker needed: background jobs run from cron (`run_jobs`, bounded 45 s, `flock`, SKIP LOCKED leases) | `scripts/netpoa/run_jobs_cron.sh`, `jobs_health_cron.sh`; `apps/core/tests/test_jobs*.py` |
| A6 | Email through a durable outbox delivered by the cron worker; console/locmem/file backends refused in production | `notifications.E001/E003`; `email_diagnostics` command |
| A7 | Static files served by WhiteNoise with hashed names (`collectstatic`); no web-served static folder | `config/settings.py` `STORAGES`; `scripts/verify_frontend_css.py` |
| A8 | HTTPS enforced, secure cookies, exact hosts and origins, proxy boundary checks | `check --deploy` errors `core.E002–E008`, `accounts.E002–E008` |
| A9 | Public registration opens only with `DJANGO_RESTRICTED_DEMO=False` + `DJANGO_PUBLIC_REGISTRATION_APPROVED=True` + site setting | `apps/accounts/tests/test_production_registration.py` |
| A10 | Manual Airtel Lipa payments off by default; refused with restricted demo or sandbox (`payments.E003/E004`); staff verification, ledger posting and single activation | `apps/payments/tests/test_manual*.py`, `test_pay02_acceptance.py` |
| A11 | Real-money rewards, withdrawals, advertiser settlement, live payments locked in code (`core.E008`) and by database triggers | `apps/core/tests/test_features.py`, `apps/payments/tests/test_pay02_acceptance.py` |
| A12 | Bunny Stream playback with signed tokens; the server calls Bunny over outbound HTTPS | `apps/videos/tests/test_providers.py`; `videos.E003/E004/W001` |
| A13 | Backup (`scripts/database/backup.sh`) and restore drill (`restore_check.sh`) scripts | scripts; restricted-demo runbooks |

## Part 2 — Hosting capabilities that still need evidence

Record the evidence (command output or NetPoa's written answer) in the Result column. Probes: run
`bash scripts/netpoa/check_host.sh <smtp host> <port>` (read-only) and the database steps of
[netpoa_compatibility.md §3](netpoa_compatibility.md).

| # | Requirement | Needed for public production | How to verify | STOP if | Result |
|---|---|---|---|---|---|
| H1 | Python 3.13 in "Setup Python App" | 3.13. 3.12/3.11/3.10 only after the full suite passes locally on that exact version | `check_host.sh` Python section; Setup Python App version list | Below 3.10 | UNVERIFIED |
| H2 | Passenger "Setup Python App" for the `.co.tz` domain (or a subdomain) as its own application | Separate app root outside `public_html` | cPanel UI; NetPoa answer | Not offered | UNVERIFIED |
| H3 | `psycopg[binary]` 3.3.6 installs in the app virtualenv (glibc wheel) | Yes | `pip install -r requirements/base.txt` on a staging app; `check_host.sh` glibc line | Neither binary wheel nor build tools work | UNVERIFIED |
| D1 | PostgreSQL server version | **15 or later** | `00_inspect.sql` | 14 or older | UNVERIFIED (only "offered" is known) |
| D2 | `btree_gist` available and trusted (or installed by NetPoa) | Yes: membership no-overlap rule | `00_inspect.sql` | Missing and NetPoa will not install it | UNVERIFIED |
| D3 | A login that owns the database, usable from the Terminal | Yes: migrations and backups | `00_inspect.sql` as the account login | No owner login | UNVERIFIED |
| D4 | A separate runtime login without owner rights | Yes: protects audit, ledger and payment history | `01`–`03` + `04_verify_runtime_role.sql` on a throwaway database | Separation cannot be proven | UNVERIFIED |
| D5 | Database backups and restore | `pg_dump` works on the host; a restore drill passes; whether cPanel backups include PostgreSQL, frequency, retention | `backup.sh`; restore drill; NetPoa answer | No working dump | UNVERIFIED |
| H4 | Cron every minute | 1 minute (5 minutes workable: slower email) | cPanel Cron Jobs UI; NetPoa answer | Longer than 5 minutes (activation and payment emails too slow) | UNVERIFIED |
| H5 | `flock`, `curl`, `psql`, `pg_dump`, `pg_restore` on the host | `psql` and `pg_dump` required | `check_host.sh` | No `psql`/`pg_dump` | UNVERIFIED |
| H6 | Outbound SMTP 587 (STARTTLS) or 465 (TLS) to the chosen provider, or NetPoa's mail server with TLS | Required: registration cannot work without email in production | `check_host.sh <smtp> <port>`; `email_diagnostics --connect`; NetPoa answer on sending limits | No TLS SMTP route | UNVERIFIED |
| H7 | Outbound HTTPS to `video.bunnycdn.com` | Required for video uploads and status | NetPoa confirmed port 443 generally; test the host name with `curl -sI https://video.bunnycdn.com` | Blocked | PARTLY VERIFIED (port 443 only) |
| H8 | Resource limits | Room for 2+ Python processes (~110 MB each), CPU for password hashing (~1 s per sign-in) | cPanel Resource Usage; NetPoa answer | Faults at idle | UNVERIFIED |
| H9 | TLS certificate for the `.co.tz` host (AutoSSL or Cloudflare Origin CA) | Yes | cPanel SSL/TLS Status | No certificate possible | UNVERIFIED |
| C1 | Cloudflare (or none) in front; what peer address Passenger sees; whether the origin can be limited to Cloudflare | Determines `DJANGO_TRUSTED_PROXY_*` | `/manage/health/` client-address diagnostics on staging; NetPoa answer on mod_remoteip/LiteSpeed | Unknown proxy behaviour with forged headers accepted | UNVERIFIED |
| C2 | `.co.tz` domain registered, DNS controllable, pointed at NetPoa (directly or via Cloudflare) | Yes | Registrar/tzNIC panel; `dig` | — | UNVERIFIED |
| H10 | Web-server access logs: retention and who can read them (emailed links appear once) | Known and limited | NetPoa answer | — | UNVERIFIED |

## Part 3 — Questions to send to NetPoa support

1. Which Python versions does "Setup Python App" offer on this account's server? Is 3.13 available?
2. Which PostgreSQL server version runs for cPanel accounts (`SELECT version();`)? Is an upgrade path to 15+ available
   if it is older?
3. Is the `btree_gist` extension available to account databases, and can the account install it (trusted
   extension), or will NetPoa install it on request?
4. Does the cPanel account's own database login own databases it creates, and can we connect with it from the
   Terminal (`psql host=localhost`)? Can additional database users be created without owner rights?
5. Are `psql`, `pg_dump` and `pg_restore` available in the account's Terminal/SSH, in which versions?
6. Do cPanel account backups include PostgreSQL databases? How often, kept how long, and how is a single database
   restored?
7. What is the minimum cron interval allowed? Are there limits on cron run time or concurrent processes?
8. May the account send email through an external SMTP provider on port 587 (STARTTLS) or 465 (TLS)? If not, what
   are NetPoa's own SMTP host, ports, TLS support and hourly sending limits? Are SPF/DKIM available for the `.co.tz`
   domain?
9. Is outbound HTTPS to `video.bunnycdn.com` allowed (no proxy/firewall rewriting)?
10. What memory, CPU, process (entry process / NPROC) and inode limits apply, and are they shared with the existing
    site on the account?
11. Does the web server (Apache/LiteSpeed) replace the client address from `X-Forwarded-For` or `CF-Connecting-IP`
    (mod_remoteip / "use client IP in header")? If so, from which networks?
12. Can a Passenger application's `.htaccess` restrict access to Cloudflare's ranges without breaking Passenger?
13. How long are raw access logs kept, and who at NetPoa can read them?
14. Is AutoSSL available for the `.co.tz` domain and its subdomains (including a staging subdomain)?
15. Will installing a Python application or cron jobs affect the existing site on the account (shared limits)?

## Part 4 — Decisions from the results

The STOP rules and fallbacks in [netpoa_compatibility.md §5](netpoa_compatibility.md) apply unchanged (for example:
PostgreSQL 14 or older, or no provable login separation → an external managed PostgreSQL 15+). Public production
adds one more:

| Result | Decision |
|---|---|
| H6 fails (no TLS SMTP) | **STOP for public launch.** The demo-only no-email waiver is not available outside the restricted demo (the activation link is the only way to verify an address), so public registration cannot work |
