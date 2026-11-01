# ClipCash

ClipCash is a planned Tanzanian video membership platform with advertiser-funded rewards.
Members pay for access to video content; any rewards are funded only by genuine, available
advertiser campaign budgets.

> **Status: authentication foundation (Phase 01).** Accounts, sign-in and account security
> are implemented. Video, membership, payment, reward and withdrawal features are **not**
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
apps/<others>/   Registered application stubs (no models or views yet)
templates/       base.html, components/, accounts/, error pages
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
| Security logging | Console `clipcash.security` logger; permanent audit records planned for Phase 02 |
| Memberships, videos, viewing, advertising, payments, rewards, wallets, withdrawals, notifications, audit, reports, support | Not implemented |

Details: [docs/architecture/authentication.md](docs/architecture/authentication.md).

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

### 4. Tests (PostgreSQL only)

```powershell
python manage.py test apps.accounts --keepdb
```

The project test runner refuses to run on anything other than PostgreSQL, refuses test
database names that are not a distinct `test_*` database, and verifies the live connection
before running. `--keepdb` reuses the provisioned database, so no `CREATEDB` privilege is
needed. Concurrency tests run only on PostgreSQL.

### 5. Migrations

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
- Emails are sent during the request; a background task system is a Phase 02 decision.
- No two-factor authentication for staff yet.
- Swahili translations are not written; the interface is English only in practice.
- Security events are logged to the console only until the Phase 02 audit app.

## Development conventions

- Line endings and encoding are defined in `.editorconfig` and `.gitattributes` (UTF-8, LF;
  Windows scripts use CRLF).
- Dependencies are pinned in `requirements/`. Add packages deliberately and pin exact versions.
- Secrets live only in `.env` or the deployment environment, never in source files.
