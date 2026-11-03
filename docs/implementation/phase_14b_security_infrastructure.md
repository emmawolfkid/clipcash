# Phase 14-B — Security and Infrastructure Readiness

**Status:** implemented, uncommitted, awaiting owner review · **Baseline:** `main` @ `aa9d4d4` (Phase 14-A commit)
**Audit findings addressed:** CC-004 (client IP / throttling), CC-005 (database privilege separation — prepared,
validation pending), CC-003 and CC-006 (worker, email, backups — readiness tooling), plus the residual Phase 14-A
provisioning race (B3). No migrations. No financial code or lock changed.

## B1 — Reverse proxy and login throttling (CC-004) — fixed

**Topology evidence.** Render's own documentation does not specify its `X-Forwarded-For` format. Public community
sources (a Render feedback thread, Render community answers and a published analysis) consistently report: requests
pass **Cloudflare, then Render's load balancer**; `X-Forwarded-For` reaches the app as `<client>, <Cloudflare edge>`
with `REMOTE_ADDR` a private load-balancer address; client-supplied `X-Forwarded-For` entries are **kept** (prefixed).
Hence the correct setting is `DJANGO_TRUSTED_PROXY_COUNT=2` (the guide's previous suggestion `1` would resolve every
visitor to a Cloudflare edge address). This is **pending verification on the real host** (tooling below).

| Change | Where |
| --- | --- |
| `resolve_client_ip()` returns `(address, resolved)`; the entry *N from the right* is used, so client-supplied prefixes are ignored; result cached per request; unresolvable cases (fewer entries than trusted proxies, invalid entry) are logged `client_ip_unresolved` | `apps/accounts/throttling.py` |
| Login, registration and email-request throttles skip **per-IP** limits when the address is unresolved instead of putting every visitor into one shared bucket; the per-account ceiling (50 failures/hour) and per-address email limits still apply | `apps/accounts/forms.py`, `apps/accounts/views.py` |
| `check --deploy`: **error** `accounts.E004` when behind a proxy (`SECURE_PROXY_SSL_HEADER` set or running on Render) with count 0; **warning** `accounts.W002` for count 1 on Render | `apps/accounts/checks.py`, `config/settings.py` (`DEPLOYED_ON_RENDER`) |
| `/manage/health/` shows Super Administrators how their own request was resolved (connection address, forwarded entries, configured count, resolved address) to verify the setting and a spoofing attempt on the real host | `apps/administration/summary.py`, `views.py`, `templates/administration/health.html` |
| Deployment test environment uses the documented count (2) | `apps/core/tests/test_deployment.py` |

Tests (`apps/accounts/tests/test_client_ip.py`): direct connections; trusted proxies; spoofed prefixes (incl.
invalid entries); misconfiguration → unresolved + logged + excluded from limits; once-per-request logging; deploy
check matrix (5 cases); 50 failures from one client do not block another client behind the same proxy; 5 failures
still lock the account for that client but not for the victim elsewhere; spoofed headers do not escape the limit;
audit records the client, not the proxy; misconfigured proxy keeps the per-account ceiling; direct connections keep
per-IP limits; health diagnostics visible to Super Administrators only.

## B2 — Database privilege separation (CC-005) — prepared; real-role validation pending

| Deliverable | Purpose |
| --- | --- |
| `scripts/database/roles/01_create_roles.sql` | `clipcash_owner` (NOLOGIN, owns everything), `clipcash_migrator` (LOGIN, member of owner, `SET role` owner so new objects are owner-owned), `clipcash_app` (LOGIN, NOINHERIT, not a member, no CREATE) |
| `02_transfer_ownership.sql` | Moves tables, sequences, views, functions in `public` to `clipcash_owner`; verification counts |
| `03_grant_runtime.sql` | App gets SELECT/INSERT/UPDATE/DELETE + sequences, never TRUNCATE/REFERENCES/TRIGGER/CREATE; **DELETE revoked on every table with a row-level BEFORE DELETE trigger** (verified: all 39 such triggers refuse deletes; the audit purge runs as owner); UPDATE revoked on append-only tables; INSERT revoked on real wallet transactions and real withdrawals; ledger accounts: column UPDATE only (`status`, `name`, `updated_at`, `debit_balance` for the balance trigger); default privileges for future tables |
| `04_verify_runtime_role.sql` | Run as `clipcash_app`: tries owner/superuser operations (disable triggers, drop the four lock triggers/constraint, replica mode, TRUNCATE audit, CREATE TABLE, replace a lock function), each in a subtransaction **forced to roll back**; checks data privileges with `has_*_privilege` (no writes, no trigger false positives); fails if anything is allowed |
| `scripts/render/predeploy.sh` | Uses `MIGRATION_DATABASE_URL` for migrate/createcachetable when set |
| `/manage/health/` "Database privilege separation" | Super Administrators see Attention while the runtime login is superuser, owns (or acts as owner of) protected tables, or may update/delete/truncate audit events or ledger lines |
| `scripts/database/production_roles.example.sql` | Marked superseded |

Local validation: the local login `clipcash_user` has **no CREATEROLE** and owns all 56 tables, so real roles could
not be created (administrator credentials were not used). `04_verify_runtime_role.sql` was executed against the
isolated **test** database as the current owner login: it reported 20 failed checks (the expected, unsafe single-owner
state — CC-005 made measurable) and **changed nothing** (trigger count 56/56, lock constraints 2/2, tables 56, audit
rows, lock-function source hash identical before and after). Health check tests: attention on the test database,
healthy path, visibility.

**Pending (owner/hosting):** run steps 01–04 on an isolated staging database with an administrator role, confirm
whether Render's database user may create roles, then production.

## B3 — Provisioning race (Phase 14-A residual) — fixed

Root cause: `SELECT … FOR UPDATE` on group rows does not block Django admin, which changes group permissions by
writing the link table `auth_group_permissions`. Fix: inside the `--apply` transaction the command takes
`LOCK TABLE auth_group_permissions IN SHARE ROW EXCLUSIVE MODE` (blocks inserts/updates/deletes and other
provisioning runs until commit; reads continue) **before** the allowlist re-check, so the check still holds at commit.
The misleading comment and docstring were corrected.

Test `ProvisioningConcurrencyTests.test_group_permission_changes_wait_for_apply` (two threads, separate PostgreSQL
connections): while `--apply` is inside its critical section, a concurrent permission insert with
`lock_timeout=300ms` is **blocked**; afterwards the lock is released and a new `--apply` refuses the now-unapproved
permission. Passed 3/3 runs. **Mutation check:** with the table lock disabled (scratchpad probe) the same test fails
(`mutate: inserted`), proving the test detects the race.

## B4 — Worker, email and backups (CC-003, CC-006) — tooling ready; services pending

| Area | Existing (reused) | Added |
| --- | --- | --- |
| Worker / scheduling | `run_jobs`, `scripts/render/run_jobs_worker.sh`, schedules, leases/retries, `jobs_status`, heartbeat on `/manage/health/`, operations admin for failed jobs | — (verified present; no gap requiring code) |
| Email | Outbox, delivery job, retries, failure audit, health email check | `manage.py email_diagnostics` (configuration without secrets; `--connect` opens/closes SMTP and sends nothing; `--send-to` queues **one** test email through the real outbox and worker); deploy warning `notifications.W001` for a non-delivering backend outside DEBUG; test email kind with no links or tokens |
| Backups | Manual `pg_dump` advice | `scripts/database/backup.sh` (custom-format dump + sha256, refuses directories inside the repository **before creating anything**, validates the archive, never prints the URL); `scripts/database/restore_check.sh` (only an empty database named `clipcash_restore_*`; checksum; restore; confirms 13 lock triggers + 2 constraints) |
| Documentation | `docs/deployment/render.md` | Updated §4 (proxy count), §5 (role separation procedure), §9 (email verification), §10 (backup and restore drill), §13 B3/B6 |

Local validation: backup of the **test** database to a scratchpad directory succeeded (56 table-data sections,
archive validated) and was deleted; refusals verified (inside repository — no directory created; missing URL;
restore into a non-`clipcash_restore_` database). A real restore drill needs a scratch database (no CREATEDB locally)
— pending. Email: tested with the locmem backend only; real SMTP not configured — pending.

## B5 — Verification

Executed 2026-11-04 on the isolated `test_clipcash_db` (development database only read).

| Command | Result |
| --- | --- |
| `python -W default manage.py test apps --keepdb -v 2` (full suite) | **952 run, 952 passed, 0 failures, 0 errors, 0 skipped; 253.8 s** (256 s wall) |
| Financial-integrity classes within that run | `DirectSqlBypassTests` 14, `ZeroMoneyTests` 3, `RealWithdrawalTests` 2, `FeatureFlagTests` 9, `FinancialInvariantTests` 2 — all passed |
| Focused: `test_client_ip`, `test_throttling`, `test_login`, `test_registration`, `test_deployment` | 94 run, OK |
| Focused: `test_provisioning` + `test_client_ip` (×3, includes the multi-connection race test) | 30 run, OK each time |
| Focused: `apps.notifications`, `apps.administration`, `apps.accounts` | 336 run, OK |
| B3 mutation probe (table lock disabled, scratchpad) | Fails as expected (`mutate: inserted`) |
| `manage.py check` / `makemigrations --check --dry-run` / `showmigrations` | No issues / No changes detected / 58 applied, 0 pending |
| `git diff --check` | Clean |

Test count 923 → 952 (+29). Warnings: only the pre-existing `RemovedInDjango60Warning` (CC-022), a Bunny-mock
`ResourceWarning` and the runner-guard `UserWarning`. No file under `apps/{wallets,withdrawals,payments,rewards,
advertising,viewing}/` and no migration changed; the 13 lock triggers and 2 constraints are untouched.

## Deferred / pending (with reasons)

| Item | Reason |
| --- | --- |
| Verify `DJANGO_TRUSTED_PROXY_COUNT=2` on the real Render service | Needs a deployment; evidence is community-sourced |
| Execute role scripts with real roles, then production | Requires an administrator role (not available locally; not to be used without approval) and Render role-creation capability |
| Paid background worker, SMTP provider, paid database plan, real restore drill | Hosting and provider decisions (CC-003, CC-006); not purchased or claimed working |
| Staff MFA, CC-014, CC-016, CC-017 (CI), CC-027 (HSTS) | Outside 14-B scope (Phases 16–17) |
| Local development DB password is weak (`clipcash`) | Local only; must never be reused on any hosted database |
| Robustness nit from the 14-A review (`fromisoformat` on a missing `expires_at`) | Low; unchanged, candidate for Phase 16 hardening |
