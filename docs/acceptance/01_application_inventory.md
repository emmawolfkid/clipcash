# 01 — Application Inventory and Architecture (Phase 13A)

**Audit date:** 2026-11-03 · **Auditor role:** Phase 13 acceptance audit (read-only) · **Scope:** whole repository at `HEAD`

> Evidence conventions used in all Phase 13 reports: file references are repository-relative with line numbers
> where useful. "Verified" means confirmed by source inspection **and** passing automated tests that exercise the
> behaviour; "inspected" means source-only. Issue IDs (`CC-0xx`) refer to
> [09_prioritized_issue_register.md](09_prioritized_issue_register.md).

## 1. Repository baseline

| Item | Observed value | How verified |
| --- | --- | --- |
| Git root | `C:/Users/emman.DESKTOP-CKBHESK/Documents/ClipCash` | `git rev-parse --show-toplevel` |
| Branch | `main` | `git branch --show-current` |
| HEAD | `a87c81b615c305451facc4d8b05c77690766a5ef` — "Add secure restricted demo email verification waiver" | `git rev-parse HEAD` |
| Working tree | Clean at audit start (only `docs/acceptance/` added by this audit) | `git status --short` |
| Phase 12 commit | **Confirmed:** `29e8214` "feat(administration): complete Phase 12 operations dashboard" | `git log --oneline` |
| Commits after Phase 12 | `6711558` (Render deployment preparation), `6938216` (public homepage + restricted-demo protections), `a87c81b` (restricted-demo email-verification waiver) | `git show --stat` |
| Historical phase commits | All 15 listed hashes (`fedb14e` … `1805406`) present in history; Phase 01 is split into `a116e16` (01A) and `18aa13d` (01B) | `git log --oneline` |
| Python | 3.13.14 (`.python-version` = 3.13) | `venv/Scripts/python.exe --version` |
| Django | 5.2.18 | `django.get_version()` |
| PostgreSQL | 18.1 (x86_64-windows), development DB `clipcash_db` | `SELECT version(), current_database()` |
| Test database | `test_clipcash_db` (default of `DB_TEST_NAME`), reused with `--keepdb` | [config/settings.py:157](../../config/settings.py#L157), [config/test_runner.py](../../config/test_runner.py) |
| Migrations | **58 applied, 0 pending**; `makemigrations --check --dry-run` → "No changes detected" | `showmigrations`, `makemigrations --check --dry-run` |
| System check | `manage.py check` → 0 issues | run during audit |
| Dependencies | Django 5.2.18, django-environ 0.14.0, psycopg[binary] 3.3.6, gunicorn 26.2.0, whitenoise 6.12.0 (+ asgiref, sqlparse, tzdata pinned). `requirements/dev.txt` adds nothing. No lock file beyond pinned `requirements/base.txt`. | `requirements/*.txt`, `pip freeze` |
| Static analysis tooling | **None configured** (no ruff/flake8/mypy/bandit config, no CI workflow, no `.github/`) | directory listing |

## 2. Project-level architecture

```
Browser ──HTTPS──> Gunicorn (sync, 2 workers) ──> Django 5.2
                                     │   middleware: HealthCheck → Security → WhiteNoise → Session → Locale → Common
                                     │               → CSRF → Auth → AccountStatus → AuditContext → SiteConfiguration
                                     │               → ContentSecurityPolicy → Messages → XFrameOptions
                                     ├── PostgreSQL 18 (single "default" alias; triggers enforce financial/audit invariants)
                                     ├── Durable job table (apps.core.jobs) ◄── `run_jobs` worker loop (scripts/render/run_jobs_worker.sh)
                                     │        └── email outbox delivery (SMTP), Bunny status sync, schedules
                                     └── Bunny Stream API (urllib, no redirects, 10 s timeout) — videos only
```

* URL root: [config/urls.py](../../config/urls.py) mounts `accounts`, `memberships`, `videos`, `advertisers`
  (advertising), `watch` (viewing), `rewards`, `payments`, `wallet`, `withdrawals`, `manage` (administration),
  `dashboard`, `admin/`, `i18n/`. **No routes exist for `notifications`, `reports` or `support`.**
* Single database alias; no read replica; cache via `DJANGO_CACHE_URL` (default per-process locmem; production
  documented as `dbcache://clipcash_cache`).
* Authentication backend: `apps.accounts.backends.AccountStatusBackend` (ACTIVE accounts only).
* Roles ([apps/accounts/roles.py](../../apps/accounts/roles.py)): `SUPER_ADMIN`, `CONTENT_MANAGER`, `MEMBER`.
  Advertiser capability is organisation membership (`AdvertiserMembership` OWNER/MANAGER), not a user role.
  All finance, review and payout permissions are custom model permissions granted by a superuser.

## 3. Module inventory

Line counts are approximate non-migration Python (tests included in the first figure, test lines second).

| App | Size (py / test lines) | Migrations | URLs | Models / key services | Jobs | DB triggers | Tests | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `accounts` | 4,366 / 2,397 | 2 | 16 routes | Custom `User` (UUID, email login, role, status, email_verified), registration, activation, confirm-email, throttled login, password reset/change, demo waiver ([access.py](../../apps/accounts/access.py)) | via notifications | — | 13 files | **Complete and verified** (core auth). Profile editing absent (CC-013) |
| `core` | 3,738 / 1,686 | 3 | home, dashboard, activity | SiteConfiguration, feature flags ([features.py](../../apps/core/features.py)), job runner, rate limits, CSP, health check, money helpers | builtin health/prune jobs | — | 10 files | **Complete and verified** |
| `audit` | 1,633 / 633 | 12 | none (admin only) | Append-only `AuditEvent`, event catalogue, context middleware, `audit_purge`, `audit_export` | — | append-only (+purge GUC) | 6 files | **Complete and verified**; DB guarantee depends on role separation (CC-005) |
| `wallets` | 2,916 / 1,239 | 5 | 2 routes | Double-entry ledger (`LedgerAccount`, `JournalEntry`, `JournalLine`), posting types, member `Wallet`, restrictions, discrepancies, reconciliation | `wallets.reconcile` | ledger guard/apply/immutable/verify; wallet posting block; transactions locked | 8 files | **Complete and verified**; money **intentionally locked** |
| `memberships` | 2,159 / 788 | 3 | 6 routes | Plans + versions, subscriptions lifecycle, access decisions, complimentary grant (CLI) | `memberships.process_expiry` | lifecycle integrity, `btree_gist` exclusion | 5 files | **Implemented and verified**, but **no in-product activation path** (CC-001) |
| `videos` | 2,092 / 922 | 2 | 3 routes (+ admin console) | Video, category, Bunny + demo providers, TUS upload auth, signed playback | `sync_status`, `delete_media`, `sweep_unfinished` | integrity | 4 files | **Implemented; Bunny verified only with mocks** (CC-002, CC-015) |
| `advertising` | 2,504 / 801 | 2 | 10 routes | Organization, memberships (OWNER/MANAGER), invitations, video grants, campaigns (revisioned), staff review, eligibility | `advertising.campaign_schedule` | org/campaign state machines; funding locked `UNFUNDED` | 4 files | **Implemented and verified**; reporting gap (CC-010) |
| `viewing` | 1,624 / 619 | 3 | 6 routes | WatchSession, heartbeat evidence, anti-fraud flags, staff review/revocation | `viewing.expire_sessions` | integrity + revocation | 3 files | **Implemented and verified** (server rules); browser path unverified (CC-015) |
| `rewards` | 1,241 / 514 | 2 | 1 route | RewardPolicy (four-eyes), DemoReward, CampaignRewardBudget, reversals | `evaluate_pending`, `reconcile_reversals` | policy/reward/budget guards | 4 files | **Implemented and verified (demo only)** |
| `payments` | 2,429 / 630 | 3 | 5 routes | PaymentIntent, ProviderEvent, SandboxPosting journal, reconciliation exceptions, refunds, disputes, advertiser sandbox funds/allocations | `expire_intents`, `poll_pending`, `check_funding` | intent/allocation/decision guards | 3 files | **Implemented and verified (sandbox only)**; disabled outside DEBUG unless explicitly allowed |
| `withdrawals` | 1,830 / 448 | 2 | 4 routes | Beneficiaries, DemoWithdrawal state machine, sandbox payouts, exceptions; real `WithdrawalRequest` table locked | `housekeeping`, `reconcile_payouts` | real requests blocked; demo state machine | 2 files | **Implemented and verified (demo only)**; real withdrawals **intentionally disabled** |
| `administration` | 2,172 / 431 | 0 | 32 routes under `/manage/` | Staff console views + summaries ([views.py](../../apps/administration/views.py), [summary.py](../../apps/administration/summary.py)) | — | — | 3 files | **Implemented and verified** |
| `notifications` | 466 / 196 | 1 | **none** | `OutboundEmail` outbox, `enqueue_email`, `deliver_email` job; 4 email kinds only | `deliver_email`, `prune_outbox` | — | 1 file | **Partially implemented** (CC-009); `views.py` is an empty scaffold |
| `reports` | 20 / 3 | 0 | **none** | Django `startapp` boilerplate only | — | — | none | **Placeholder / stub** (CC-007) |
| `support` | 20 / 3 | 0 | **none** | Django `startapp` boilerplate only | — | — | none | **Placeholder / stub** (CC-008) |

Root `tests/` contains only `.gitkeep`; all tests live in `apps/*/tests/`.

## 4. Dependency map (application level)

```
accounts ◄──────────────── every app (User, access mixins, member_eligibility via memberships)
core (features, jobs, ratelimit, money) ◄── every app
audit (record_business_event / record_security_event) ◄── every app
notifications ◄── accounts (4 email kinds)
memberships ◄── videos (authorize_playback), advertising (campaign_eligibility), payments (intents/preview)
videos ◄── advertising (video grants), viewing (playback)
advertising ◄── viewing (eligibility), rewards (campaign/policies), payments (deposits/allocations)
viewing ◄── rewards (verified sessions, reversal signal)
wallets (ledger, posting_types) ◄── payments (posting-type rules only; writes SandboxPosting, never JournalLine)
                                 ◄── withdrawals (eligibility reads wallet balances = 0)
payments.providers.sandbox_active ◄── withdrawals (payout simulator), accounts (demo waiver gate)
administration ──► all of the above (read summaries + calls owning services)
rewards ──X── wallets   (no import: demo rewards have no path to the ledger)
payments.SandboxCampaignAllocation ──X── rewards (no link: CC-011)
viewing.verified_views() ──X── any caller (unused: CC-010)
reports, support ──X── everything (empty)
```

## 5. Background jobs and schedules

Registered schedules (interval seconds): `advertising.campaign_schedule` 60, `viewing.expire_sessions` 60,
`rewards.evaluate_pending` 60, `payments.expire_intents` 60, `payments.poll_pending` 300,
`videos.sweep_unfinished` 300, `withdrawals.reconcile_payouts` 300, `memberships.process_expiry` 900,
`rewards.reconcile_reversals` 900, `payments.check_funding` 3600, `wallets.reconcile` 3600,
`withdrawals.housekeeping` 3600, plus built-ins in [apps/core/jobs/builtin.py](../../apps/core/jobs/builtin.py)
(health check, stale-job detection, rate-limit and finished-job pruning). Jobs are claimed with `SKIP LOCKED`,
leased (300 s), retried with backoff, and run by `manage.py run_jobs` (cron locally; a Render Background Worker
loop in [scripts/render/run_jobs_worker.sh](../../scripts/render/run_jobs_worker.sh)).

## 6. Unused, dead or unreachable code

| Item | Evidence | Classification |
| --- | --- | --- |
| `apps/reports/*`, `apps/support/*` | Boilerplate `models.py`/`views.py`/`tests.py`; no URLs, no models, no migrations | Placeholder (CC-007, CC-008, CC-023) |
| `apps/notifications/views.py` | `from django.shortcuts import render` only | Dead scaffold (CC-023) |
| `apps.viewing.services.verified_views()` | [apps/viewing/services.py:658](../../apps/viewing/services.py#L658); no caller outside tests | Unused integration point (CC-010) |
| `apps.memberships.services.activate_with_verified_payment()` / `register_payment_verifier()` | No verifier registered anywhere; requires locked `live_payments` | Intentionally dormant boundary |
| `apps.wallets.wallet_services.request_wallet_posting()` | Always raises `WalletPostingLocked` | Intentionally disabled |
| `apps.withdrawals.services.request_withdrawal()` | Always refuses; DB trigger blocks the table | Intentionally disabled |
| Templates | Static check: all 218 named URL patterns referenced from templates resolve; every template under `templates/` (except Django-admin overrides) is referenced by code or another template | No orphans found |

## 7. Undocumented or notable assumptions

1. **Membership activation** is only possible through the `grant_complimentary_membership` management command
   with `DJANGO_MEMBERSHIPS_COMPLIMENTARY_GRANTS=True` (CC-001). Sandbox membership payments create a
   `MembershipActivationPreview` and never activate ([apps/payments/services.py:488-512](../../apps/payments/services.py#L488-L512)).
2. **Viewing is gated by the `advertisers` feature flag**, not by its own flag ([apps/viewing/views.py:25](../../apps/viewing/views.py#L25)) (CC-026).
3. **Separation-of-duties workflows need multiple privileged staff** (reward-policy four-eyes, refund approval,
   demo withdrawal creator ≠ approver ≠ payout initiator, high-risk two approvers) while only superusers can
   grant those permissions (CC-012).
4. **The sandbox payout simulator depends on sandbox payments being active**
   ([apps/withdrawals/payouts/__init__.py:10](../../apps/withdrawals/payouts/__init__.py#L10)), and the
   restricted-demo email waiver requires sandbox payments to be **inactive**
   ([apps/accounts/access.py:13-25](../../apps/accounts/access.py#L13-L25)). Both cannot be demonstrated on the
   same deployment.
5. The README already declares "Live payments, real rewards, real withdrawals, reports, support — Not implemented"
   ([README.md:73](../../README.md#L73)), and the Render guide lists blockers B1–B7
   ([docs/deployment/render.md §13](../deployment/render.md)).

## 8. Areas not inspected line-by-line

The audit inspected every app's services, views, URLs, integrity migrations and access helpers, and sampled
templates/forms. The following were reviewed only at outline level and are **not** claimed as fully inspected:
`apps/*/admin.py` (beyond accounts/group admin), `apps/videos/services.py` upload/TUS internals,
`apps/withdrawals/services.py` risk-flag and limit arithmetic, `apps/core/jobs/services.py` lease mechanics,
all 108 HTML templates individually (scanned by pattern), and `static/css/clipcash.css`.
