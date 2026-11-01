# Audit infrastructure (Phase 02B)

Status: implemented in `apps/audit`. Audit events are the permanent record of
security-relevant and administrative actions. The console `clipcash.security`
logger remains as an operational log (one line per audit event, ids and codes
only) and for operational warnings such as throttling and cache errors.

## Event model (`audit.AuditEvent`)

| Field | Purpose |
| --- | --- |
| `id` | Random UUID |
| `occurred_at` | When the event was created (UTC) |
| `category`, `action` | Registered code, e.g. `ACCOUNT` / `account.status_changed` |
| `outcome` | `SUCCESS`, `FAILURE` or `DENIED` |
| `source` | `WEB`, `ADMIN`, `SYSTEM` or `CLI` |
| `actor_id`, `actor_email`, `actor_role` | Who acted, as a snapshot at event time |
| `target_app_label`, `target_model`, `target_id`, `target_repr` | What was affected, as a snapshot |
| `request_id` | Correlation id; one per request or command run, returned as `X-Request-ID` |
| `ip_address`, `user_agent` | Optional; controlled by `DJANGO_AUDIT_RECORD_IP_ADDRESS` / `..._USER_AGENT` |
| `metadata` | Validated JSON, keys allowlisted per action |
| `idempotency_key` | Optional; unique when set |

Actor and target are **not foreign keys**: deleting an account or object never
deletes or blanks its audit history, and the snapshot keeps records readable.

### What is never stored

Passwords, tokens, session or CSRF cookies, payment credentials and request
bodies. The service enforces this by:

* an allowlist of metadata keys per registered action;
* rejecting keys containing `password`, `secret`, `token`, `cookie`, `session`,
  `csrf`, `otp`, `pin`, `card`, `cvv`, `iban`, `account_number`, `authorization`,
  `api_key` or `private` at any depth, even if allowlisted by mistake;
* limiting values to short strings, integers, booleans, `null`, small lists and
  shallow objects (no floats);
* storing a keyed hash (`identifier_hash`) instead of the email typed at sign-in
  or password reset. Users sometimes type a password into the email field; the
  raw value is never kept. Attempts whose input is not a valid email address
  never reach authentication and are not recorded at all.

Actor email snapshots are personal data, kept to make records meaningful after
account deletion; access to the audit workspace is restricted accordingly.

## Event catalogue

| Action | Kind | Recorded when |
| --- | --- | --- |
| `auth.login.succeeded` / `.failed` / `.throttled` | security | Public and admin sign-in |
| `auth.logout` | security | Sign-out |
| `auth.password_reset.requested` | security | Reset form submitted (throttled or not) |
| `auth.password_reset.completed`, `auth.password.changed` | business | New password saved |
| `account.registration.started` | business | New PENDING account created |
| `account.registration.repeated` | security | Registration for an existing address |
| `account.activated`, `account.email_confirmed` | business | Activation / confirmation |
| `account.created_by_staff`, `account.deleted` | business | Admin create / delete |
| `account.role_changed`, `account.status_changed` | business | Any role or status change (suspension, reactivation, disabling) |
| `account.access_change.denied` | security | Any rejected role/status change (privilege-escalation attempts) |
| `authorization.denied` | security | Any other `PermissionDenied` on a write request |
| `config.updated` | business | One per configuration version; field changes, feature flags and maintenance mode |
| `config.update.denied` | security | Unauthorized or non-editable configuration change |
| `audit.exported`, `audit.purged` | security / business | Archive and retention commands |

## Transaction and failure handling

**Business events** (`record_business_event`) must be called inside the
business transaction; the service refuses otherwise. The event commits or rolls
back with the change. Any failure (validation or database) raises, so the
business change rolls back too: no change can commit without its audit record.

**Security events** (`record_security_event`) must survive the rollback of the
operation they describe. Outside a transaction they are written immediately.
Inside one, with an active audit context, they are held in memory and written
by `AuditContextMiddleware` (or `audit_scope()` for commands and future jobs)
after the view and its transactions have finished. A write failure is logged
(`audit_write_failed`) and does not break the response, because a failed
sign-in page must still render.

Limitation: code that runs inside a transaction **without** an audit context
writes security events immediately, within that transaction. Use
`audit_scope()` in management commands and background jobs.

Duplicates are avoided by construction: each action has one call site, the
generic `authorization.denied` is skipped when a specific denial was already
recorded for the request, and `config.updated` uses the version number as its
idempotency key.

## Immutability

| Layer | Protection |
| --- | --- |
| Model | `save()` on an existing row and `delete()` raise `AuditImmutableError` |
| QuerySet | `update()`, `delete()`, `bulk_update()`, `update_or_create()` raise |
| Admin | No add, change, delete or bulk actions |
| Database | Trigger `audit_auditevent_append_only` rejects every UPDATE and every DELETE unless the transaction sets `clipcash.audit_purge = 'on'` (only `audit_purge` does) |
| Database | Check constraints on outcome, source, category and action format |

**Limits of these protections.** The trigger stops application code, ORM
mistakes and SQL issued through the application. It does not stop the table
owner or a superuser, who can disable the trigger, set the purge flag or
`TRUNCATE` the table. TRUNCATE is not blocked because Django's test runner
needs it. Locally the application role owns the tables, so these protections
guard against mistakes, not against someone holding the database credentials.

### Production role separation (recommended for Phase 16)

`scripts/database/production_roles.example.sql` is a reviewed template (not run
anywhere). It defines:

* `clipcash_owner`: owns the schema and runs migrations; used only by deployment.
* `clipcash_app`: the web application's runtime role; `SELECT, INSERT` only on
  `audit_auditevent` (no `UPDATE`, `DELETE`, `TRUNCATE`), normal access elsewhere.
* Retention purges run as `clipcash_owner` through `audit_purge`.

With this split, a compromised web application cannot alter or erase history.
Off-site, write-once archives (see below) protect against the owner role too.

## Retention and archival

* Minimum retention is `AUDIT_MIN_RETENTION_DAYS` = 365 days; `audit_purge`
  refuses anything newer. The actual policy (for example 2 years for sign-in
  activity, 7+ years for anything later linked to money) is an **open decision**
  for legal review under Tanzanian requirements.
* `python manage.py audit_export --since YYYY-MM-DD --until YYYY-MM-DD --output file.jsonl`
  writes one JSON object per line, refuses to overwrite and records `audit.exported`.
* `python manage.py audit_purge --before YYYY-MM-DD [--confirm]` is a dry run unless
  confirmed, and records `audit.purged` in the same transaction.
* Archive files should be stored off the application server in write-once
  storage, with checksums.

## Audit workspace (admin)

Admin → Audit events, read-only. Access requires an ACTIVE staff account with
`audit.view_auditevent`; Super Administrators have it implicitly, anyone else
must be granted it explicitly. Filters: outcome, category, source, action, actor
role, target model, date hierarchy, plus `?actor_id=`, `?target_id=` and
`?request_id=` for exact matches. Search: actor email, target id or name, request
id. 50 rows per page, without a full count, for large tables. The detail view
shows all fields with metadata pretty-printed; nothing beyond what was recorded.

## Performance and growth

* Inserts only; UUID keys avoid hot spots; no locks are taken.
* Indexes cover newest-first browsing and filters: `occurred_at`,
  `(actor_id, occurred_at)`, `(action, occurred_at)`, `(outcome, occurred_at)`,
  `(target_model, target_id)` and `request_id`, plus the partial unique index on
  `idempotency_key`.
* Volume is dominated by sign-in events. When the table reaches tens of millions
  of rows, add monthly range partitioning on `occurred_at`, which also makes
  purging cheap (drop old partitions).
