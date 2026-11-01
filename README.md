# ClipCash

ClipCash is a planned Tanzanian video membership platform with advertiser-funded rewards.
Members pay for access to video content; any rewards are funded only by genuine, available
advertiser campaign budgets.

> **Status: early foundation.** No user-facing features are implemented yet. Real-money
> rewards, payments and payouts are not implemented and must remain disabled until legal,
> payment-provider and operational requirements are satisfied. Membership fees are not an
> investment and do not guarantee any return.

## Technology stack

| Component      | Version / choice                          |
| -------------- | ----------------------------------------- |
| Python         | 3.13                                      |
| Django         | 5.2 (LTS)                                 |
| Database       | PostgreSQL 18 (driver: psycopg 3)         |
| Configuration  | django-environ, `.env` file               |
| Video provider | Bunny Stream (planned, not yet integrated)|
| Frontend       | Server-rendered Django templates (planned)|

## Project layout

```
config/          Django project (settings, URLs, WSGI/ASGI)
apps/            Django applications
  accounts/      Custom user model (email login, UUID primary key, role, status)
  core/ administration/ memberships/ videos/ viewing/ advertising/ payments/
  rewards/ wallets/ withdrawals/ notifications/ audit/ reports/ support/
                 Registered application stubs (no models or views yet)
requirements/    Pinned dependencies (base.txt, dev.txt)
templates/ static/ docs/ scripts/ tests/
                 Directory scaffold (currently empty)
```

## Current implementation status

| Area                          | Status                                                        |
| ----------------------------- | ------------------------------------------------------------- |
| Project settings              | Environment-driven; development configuration only            |
| Custom user model             | Hardened (Phase 01A): normalized email, optional E.164 phone, role/status invariants enforced by the model and database constraints. Initial migration generated. **Not yet applied.** |
| Django admin for users        | Registered; access flags read-only. Privilege-escalation hardening pending (Phase 01B) |
| Account status enforcement    | **Partial**: non-ACTIVE accounts fail password authentication, but existing sessions are not ended and no dedicated backend or middleware exists yet (Phase 01B) |
| Localization                  | English and Swahili configured; no translations written yet   |
| Registration, login, password reset views | Not implemented                                 |
| Memberships, videos, viewing, advertising | Not implemented                                 |
| Payments, rewards, wallets, withdrawals   | Not implemented                                 |
| Notifications, audit, reports, support    | Not implemented                                 |
| Automated tests               | Accounts model, manager and phone tests; require PostgreSQL test-database access (see below) |
| Production deployment config  | Not implemented                                               |

## Local setup (Windows 11)

### 1. Python environment

```powershell
py -3.13 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements\dev.txt
```

### 2. PostgreSQL

Create a dedicated role and database (run as a PostgreSQL administrator, e.g. in `psql`):

```sql
CREATE ROLE clipcash_user WITH LOGIN PASSWORD 'choose-a-strong-password';
CREATE DATABASE clipcash_db OWNER clipcash_user ENCODING 'UTF8';
```

The test suite runs against PostgreSQL. The preferred least-privilege setup is a
pre-created test database owned by the application role, reused with `--keepdb`, so the
role never needs `CREATEDB`:

```sql
CREATE DATABASE test_clipcash_db OWNER clipcash_user ENCODING 'UTF8';
```

```powershell
python manage.py test --keepdb
```

Do not grant `CREATEDB` or superuser rights to application roles in production.

### 3. Environment variables

```powershell
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Paste the generated value into `DJANGO_SECRET_KEY` in `.env` and set your database password.
`.env` is ignored by Git and must never be committed.

| Variable               | Required | Default               | Description                                  |
| ---------------------- | -------- | --------------------- | -------------------------------------------- |
| `DJANGO_SECRET_KEY`    | Yes      | none                  | Unique, random secret. Never reuse or share. |
| `DJANGO_DEBUG`         | No       | `False`               | `True` for local development only.           |
| `DJANGO_ALLOWED_HOSTS` | No       | `127.0.0.1,localhost` | Comma-separated host names.                  |
| `DB_NAME`              | Yes      | none                  | PostgreSQL database name.                    |
| `DB_USER`              | Yes      | none                  | PostgreSQL role.                             |
| `DB_PASSWORD`          | Yes      | none                  | PostgreSQL role password.                    |
| `DB_HOST`              | No       | `127.0.0.1`           | PostgreSQL host.                             |
| `DB_PORT`              | No       | `5432`                | PostgreSQL port.                             |

### 4. Verify the setup

These commands do not modify the database:

```powershell
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py showmigrations
python -m pip check
```

Do **not** run `python manage.py migrate` until the custom user model has been finalized in
Phase 01. Once migrations are applied, changes to the initial user migration require a new
migration instead of regeneration.

## Development conventions

- Line endings and encoding are defined in `.editorconfig` and `.gitattributes` (UTF-8, LF;
  Windows scripts use CRLF).
- Dependencies are pinned in `requirements/`. Add packages deliberately and pin exact versions.
- Secrets live only in `.env` or the deployment environment, never in source files.
