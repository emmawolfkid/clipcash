# 07 — Infrastructure and Operational Readiness (Phase 13G)

**Scope:** repository configuration and documentation only. **No external service was accessed** (Render, Bunny,
SMTP, payment providers). The Render account state, database plan and creation date are therefore **unverified**.

## 1. Readiness matrix

| Area | Repository evidence | Status | Issue |
| --- | --- | --- | --- |
| PostgreSQL requirements | PostgreSQL only (runner + `DATABASE_URL` check refuse others); `btree_gist` extension needed (memberships 0001); triggers/constraint triggers | ✅ documented ([render.md §7](../deployment/render.md)) | — |
| Database roles / least privilege | Template [scripts/database/production_roles.example.sql](../../scripts/database/production_roles.example.sql) (owner vs runtime role, revoke UPDATE/DELETE/TRUNCATE on ledger and audit) — **not applied**; Render uses a single owner credential | ⛔ | CC-005 |
| Connection security | `DJANGO_DB_SSLMODE=require` recommended; `core.W002` warns for remote DB without TLS; `CONN_MAX_AGE=60`, health checks on | ✅ (config) | — |
| Environment variables | `.env.example` complete; Render variable table in render.md §4; `.env` git-ignored | ✅ | — |
| Secret management | Secrets only from env; `__repr__` hides Bunny keys; no secrets in repo (`.env` ignored; not read into reports) | ✅ | — |
| Web process | Gunicorn sync ×2, 60 s timeout, access log off, `max_requests` recycling ([config/gunicorn.conf.py](../../config/gunicorn.conf.py)) | ✅ | — |
| Worker process | `scripts/render/run_jobs_worker.sh` loop with SIGTERM handling; Render Background Worker is **paid-only** | ⛔ until provisioned | CC-003 |
| Job scheduling / retries / recovery | Lease 300 s, backoff, `on_failure`, stale detection, admin retry, `jobs_status` JSON | ✅ (code) | — |
| Bunny Stream | Provider implemented; credentials unset (`videos.W001`); CDN token auth must be enabled in Bunny | ⛔ for real playback | CC-002, CC-025 |
| Email delivery | Outbox + SMTP backend via env; provider not chosen; free Render instances block SMTP | ⛔ until configured | CC-003 (B2) |
| Static files | WhiteNoise + `CompressedManifestStaticFilesStorage` outside DEBUG; `collectstatic` in build | ✅ | — |
| Media | No user media stored locally (videos go to Bunny) | ✅ | — |
| HTTPS / proxy | `SECURE_PROXY_SSL_HEADER`, `SECURE_SSL_REDIRECT` via env; health probe exempt | ✅ (config) | — |
| Client IP behind proxy | `DJANGO_TRUSTED_PROXY_COUNT` defaults 0; Render XFF semantics unverified | ⛔ verification needed | CC-004 |
| Allowed hosts / CSRF origins | Render hostname auto-added; custom domains via env | ✅ | — |
| Security headers / CSP | See [04_security_findings.md](04_security_findings.md); HSTS deliberately 0 initially | ✅ / ⚠️ | CC-027 |
| Logging | Console handlers; `clipcash.security` / `clipcash.jobs` loggers; token-path redaction | ✅ | — |
| Error monitoring / alerting | None (no Sentry or equivalent; heartbeat visible only in `/manage/health/`) | ⛔ | CC-016 |
| Backups | Free Render DB: none. Manual `pg_dump` procedure documented | ⚠️ | CC-006, CC-017 |
| Restore procedure | Documented (restore into new DB, repoint `DATABASE_URL`) — **never rehearsed** | ⚠️ | CC-017 |
| Health checks | `/healthz` (DB `SELECT 1`, no-store, reveals nothing) | ✅ | — |
| CI/CD | **No CI workflow**; Auto-Deploy recommended Off until verified | ⚠️ | CC-017 |
| Operational documentation | Render guide (388 lines) incl. rollback, troubleshooting, blockers B1–B7; architecture docs per module | ✅ | — |

## 2. Can the proposed hosting support the required services?

| Required service | Render offering | Constraint |
| --- | --- | --- |
| Web app | Web Service (free or paid) | Free: spin-down, no pre-deploy command, no shell, SMTP blocked |
| Background jobs | Background Worker | **No free tier** — without it: no emails (no account activation), no session expiry, no scheduled campaign activation, no automatic reward evaluation, no membership expiry |
| PostgreSQL 18 with triggers + `btree_gist` | Render Postgres | Free plan expires 30 days after creation, then deleted after a 14-day grace period; no backups; 1 GB |
| Shared cache / rate limits | DB cache table (`createcachetable` in pre-deploy) | Pre-deploy command is paid-only; on free, run manually (§7.2) |
| Video | Bunny Stream (external) | Credentials + CDN token auth required |

**Conclusion:** a *usable* restricted demo on Render needs at minimum a paid web service, a paid background
worker and an SMTP provider (or demo accounts provisioned without email, using the documented waiver), plus a
video source. A paid database is required for anything kept beyond 30 days.

## 3. Local development vs restricted demo deployment

| Concern | Local (DEBUG=True) | Restricted Render demo (DEBUG=False) |
| --- | --- | --- |
| Registration | Open (unless configured) | **Closed** (`RESTRICTED_DEMO_MODE` default on) |
| Email | Console backend | SMTP required; worker required |
| Payments | `sandbox` by default | `disabled` (sandbox needs explicit `DJANGO_PAYMENTS_SANDBOX_ALLOWED`) |
| Demo payouts simulator | Available | **Unavailable** while payments disabled |
| Email waiver | n/a | Available only while payments are disabled |
| Video provider | `demo` (placeholder or sample URL) | `bunny`; `demo` only with `VIDEO_DEMO_ALLOWED` (documented as "never on a public deployment") |
| Throttle backend | cache | database |
| Static files | Django | WhiteNoise manifest (missing file → 500) |

## 4. Render PostgreSQL Free-plan expiry concern

The repository documents the risk ([render.md §10, §13 B4](../deployment/render.md)): Free Render Postgres
expires 30 days after creation and is deleted after a further 14-day grace period, with no backups. **The plan
and creation date of `clipcash-db` could not be verified** (no external access authorised). Action: confirm in
the Render dashboard, take a `pg_dump` immediately if the database holds anything worth keeping, and upgrade
before demo data is prepared (CC-006).

## 5. Recommended operational acceptance steps

1. Confirm DB plan/region/creation date; upgrade or record expiry date.
2. Provision the paid worker; verify `/manage/health/` heartbeat.
3. Configure SMTP on web **and** worker; send an activation email end-to-end.
4. Resolve client-IP handling (B3) with the documented spoofing test.
5. Apply role separation (template) on a staging copy and run the full test suite against that role model.
6. Add error monitoring and a heartbeat alert.
7. Rehearse a backup + restore into a fresh database.
