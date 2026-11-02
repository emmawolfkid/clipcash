# ClipCash

ClipCash is a planned Tanzanian video membership platform with advertiser-funded rewards.
Members pay for access to video content; any rewards are funded only by genuine, available
advertiser campaign budgets.

> **Status: platform foundations (Phases 01–02A).** Accounts, sign-in, account security, site
> configuration, feature flags, money conventions, permanent audit records, the double-entry ledger foundation and background processing are implemented. Video, membership, payment, reward and withdrawal features are **not**
> implemented. Real-money rewards, payments and payouts must remain disabled until legal,
> payment-provider and operational requirements are satisfied. Membership fees are not an
> investment and do not guarantee any return.

## Technology stack

| Component      | Version / choice                                  |
| -------------- | ------------------------------------------------- |
| Python         | 3.13                                              |
| Django         | 5.2 (LTS)                                         |
| Database       | PostgreSQL 18 (driver: psycopg 3)                 |
| Configuration  | django-environ, `.env` file                       |
| Video provider | Bunny Stream (planned, not yet integrated)        |
| Frontend       | Server-rendered Django templates, `static/css/clipcash.css` |

## Project layout

```
config/          Settings, URLs, WSGI/ASGI, PostgreSQL-only test runner
apps/accounts/   Users, authentication, account security (see docs/architecture/authentication.md)
apps/core/      Base models, site configuration, feature flags, money (see docs/architecture/core.md)
apps/administration/  Admin branding and configuration admin
apps/audit/     Append-only audit events, audit workspace, export/purge (see docs/architecture/audit.md)
apps/wallets/   Double-entry ledger: chart of accounts, posting service, DB integrity (see docs/architecture/ledger.md)
apps/notifications/  Durable email outbox (see docs/architecture/background-processing.md)
apps/memberships/  Plans, versions, subscriptions, access checks (see docs/architecture/memberships.md)
apps/<others>/   Registered application stubs (no models or views yet)
templates/       base.html, components/, accounts/, core/, error pages
static/css/      Site stylesheet
scripts/database/  Reviewed PostgreSQL provisioning scripts
docs/            Architecture and security documentation
requirements/    Pinned dependencies (base.txt, dev.txt)
```

## Current implementation status

| Area | Status |
| --- | --- |
| Custom user model | UUID primary key; normalized, case-insensitively unique email; optional E.164 phone; role and status with derived access flags enforced by database constraints |
| Registration | Email only; the inbox owner sets the password from a single-use link (prevents password planting); anti-enumeration responses |
| Sign-in / sign-out | Email login (case-insensitive), ACTIVE accounts only, rate limited, POST-only sign-out with confirmation page |
| Passwords | Reset (ACTIVE accounts, 1 h single-use links) and change; both end other sessions |
| Account status | PENDING / ACTIVE / SUSPENDED / DISABLED with validated transitions; suspension ends sessions on the next request |
| Roles | SUPER_ADMIN, CONTENT_MANAGER, MEMBER with synced Django Groups; centralized role/status service; admin privilege-escalation protections |
| Rate limiting | Cache-backed; shared cache required (startup check) when `DEBUG` is off |
| Localization | English and Swahili selectable; strings marked for translation. **No Swahili translations yet** (requires GNU gettext and review) |
| Site configuration | Single database row editable only by ACTIVE Super Administrators: branding, support contacts, regional defaults, maintenance mode, business feature flags; validated and versioned |
| Feature flags | Registration (on), memberships / videos / advertisers (off); real-money rewards, live payments and withdrawals **locked off in code** |
| Money | Integer whole-shilling TZS amounts, immutable `Money` type, no floats; no balances or ledger yet |
| Audit | Append-only audit events (database trigger + ORM guards) for sign-in, account, access-control and configuration actions; read-only admin workspace; export and retention commands |
| Ledger | Double-entry journal with database-enforced balance, immutability, overdraft protection and funding separation; read-only admin. **All posting types locked off: no real money, balances, rewards or withdrawals** |
| Background processing | PostgreSQL job queue (SKIP LOCKED, leases, retries with jitter), cron-driven `run_jobs`, scheduled tasks, `jobs_status` health check, operations admin |
| Email | Durable outbox written in the business transaction; tokens generated at delivery and dated to the request; never stored |
| Memberships | Plans with immutable price versions; subscription lifecycle enforced by the database; member pages; access-check service. **Paid activation waits for verified payments (not available yet)** |
| Videos | Categories and catalogue with separate processing/publication status; Bunny Stream (direct TUS upload, status sync jobs, short-lived signed playback) or a labelled demo provider; administration console at `/manage/`; membership-gated library at `/videos/`; member dashboard at `/dashboard/`. **Watching is not verified viewing and earns nothing** |
| Advertising | Advertiser organizations with reviewed verification, team invitations and tenant isolation; staff-authorized campaign videos; campaign lifecycle with revision-bound approval, scheduling job and server-side eligibility; advertiser pages at `/advertisers/`, review queues at `/manage/`. **Budgets are demonstration figures only: no funding, ledger entries or rewards** |
| Verified viewing | Server-owned watch sessions for sponsored campaigns (`/watch/`): eligibility re-checked on every heartbeat, watch time capped by server-measured time, continuous-coverage rule, rule-based fraud flags, staff review at `/manage/viewing/`, database-enforced once-only counting. **A verified view is evidence, not proof of attention, and earns nothing** |
| Demo rewards | Versioned, four-eyes-approved demo reward policies per approved campaign revision; one simulated reward decision per verified view; atomic simulated budget; reversals tied to viewing revocations; member history at `/rewards/`, console at `/manage/rewards/`. **Simulated records only: not money, never in the ledger, not withdrawable** |
| Payments, real rewards, withdrawals, reports, support | Not implemented |

Details: [docs/architecture/authentication.md](docs/architecture/authentication.md), [docs/architecture/core.md](docs/architecture/core.md), [docs/architecture/audit.md](docs/architecture/audit.md), [docs/architecture/ledger.md](docs/architecture/ledger.md), [docs/architecture/background-processing.md](docs/architecture/background-processing.md), [docs/architecture/memberships.md](docs/architecture/memberships.md), [docs/architecture/videos.md](docs/architecture/videos.md), [docs/architecture/advertising.md](docs/architecture/advertising.md), [docs/architecture/viewing.md](docs/architecture/viewing.md), [docs/architecture/rewards.md](docs/architecture/rewards.md).

## Local setup (Windows 11)

### 1. Python environment

```powershell
py -3.13 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements\dev.txt
```

### 2. PostgreSQL

Create the application role and development database (as a PostgreSQL administrator):

```sql
CREATE ROLE clipcash_user WITH LOGIN PASSWORD 'choose-a-strong-password';
CREATE DATABASE clipcash_db OWNER clipcash_user ENCODING 'UTF8';
```

Provision the disposable **test** database with the reviewed, idempotent script. It creates
`test_clipcash_db` owned by `clipcash_user`, revokes access from other roles, and grants no
`CREATEDB`/superuser rights. You are prompted for the administrator password:

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h 127.0.0.1 -d postgres -f scripts\database\create_test_database.sql
```

### 3. Environment variables

```powershell
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Paste the generated value into `DJANGO_SECRET_KEY` and set `DB_PASSWORD`. `.env` is ignored
by Git and must never be committed. All options are listed in `.env.example`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | required | Unique random secret |
| `DJANGO_DEBUG` | `False` | `True` only for local development |
| `DJANGO_ALLOWED_HOSTS` | `127.0.0.1,localhost` | Served host names |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD` | required | Application database |
| `DB_HOST`, `DB_PORT` | `127.0.0.1`, `5432` | Database server |
| `DB_TEST_NAME` | `test_clipcash_db` | Disposable test database |
| `DJANGO_CACHE_URL` | `locmemcache://clipcash` | Cache; use a shared cache in production |
| `DJANGO_THROTTLE_REQUIRE_SHARED_CACHE` | `not DEBUG` | Fail startup if the cache is per-process |
| `DJANGO_TRUSTED_PROXY_COUNT` | `0` | Trusted reverse proxies for client IPs |
| `DJANGO_SECURE_COOKIES` | `not DEBUG` | HTTPS-only session and CSRF cookies |
| `DJANGO_SESSION_COOKIE_AGE` | `43200` | Session lifetime (seconds) |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | empty | HTTPS origins allowed to POST |
| `DJANGO_SECURE_SSL_REDIRECT`, `DJANGO_SECURE_HSTS_SECONDS`, `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS`, `DJANGO_SECURE_PROXY_SSL_HEADER` | off | HTTPS hardening for production |
| `DJANGO_EMAIL_BACKEND`, `DJANGO_DEFAULT_FROM_EMAIL` | console backend | Outgoing email |
| `DJANGO_SECURITY_LOG_LEVEL` | `INFO` | `clipcash.security` log level |
| `DJANGO_AUDIT_RECORD_IP_ADDRESS`, `DJANGO_AUDIT_RECORD_USER_AGENT` | `True` | Store client IP / user agent on audit events |

### 4. Tests (PostgreSQL only)

```powershell
python manage.py test apps --keepdb
```

The project test runner refuses to run on anything other than PostgreSQL, refuses test
database names that are not a distinct `test_*` database, and verifies the live connection
before running. `--keepdb` reuses the provisioned database, so no `CREATEDB` privilege is
needed. Always pass `--keepdb`: without it Django tries to drop and re-create the test
database, which the application role is not allowed to do. The runner also restores the
role groups and site configuration row before each run, because `--keepdb` does not
re-run data migrations. Concurrency tests run only on PostgreSQL.

### 5. Background jobs and email

Emails and maintenance tasks are processed by a bounded command. Locally, run it whenever
you need queued email delivered (the console email backend prints it):

```powershell
python manage.py run_jobs
python manage.py jobs_status     # JSON health report; exit code 1 when unhealthy
```

In production, cron runs `run_jobs` every minute (see
[docs/architecture/background-processing.md](docs/architecture/background-processing.md)).

### 6. Migrations

```powershell
python manage.py check
python manage.py makemigrations --check --dry-run   # must report "No changes detected"
python manage.py showmigrations                      # review applied state
python manage.py migrate --plan                      # review before applying
python manage.py migrate
python manage.py createsuperuser                     # the only way to create a Super Administrator
```

Never reset, drop or fake migrations. After migrations are applied anywhere, every model
change needs a new migration.

## Security configuration (production)

Set `DJANGO_DEBUG=False` and, at minimum: a shared cache (`DJANGO_CACHE_URL`, e.g.
`dbcache://clipcash_cache` followed by `python manage.py createcachetable`),
`DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, HTTPS settings, and a real email
backend. Then run:

```powershell
python manage.py check --deploy
```

It must report no `accounts.*` errors. Deployment hardening is completed in Phase 16.

## Known limitations

- Rate limiting is per account+IP, per account and per IP; very large distributed attacks
  need upstream protection (WAF/CDN).
- Emails are delivered by `run_jobs` (cron); without cron running, no email is sent. Run
  `python manage.py run_jobs` locally to deliver queued email to the console backend.
- No two-factor authentication for staff yet.
- Real Bunny Stream playback protection (embed + CDN token authentication) must be verified
  against a real library before go-live; automated tests never call Bunny.
- Swahili translations are not written; the interface is English only in practice.
- Audit protections can be bypassed by the database owner; production needs the role
  separation in `scripts/database/production_roles.example.sql`.

## Development conventions

- Line endings and encoding are defined in `.editorconfig` and `.gitattributes` (UTF-8, LF;
  Windows scripts use CRLF).
- Dependencies are pinned in `requirements/`. Add packages deliberately and pin exact versions.
- Secrets live only in `.env` or the deployment environment, never in source files.
