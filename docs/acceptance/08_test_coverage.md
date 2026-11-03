# 08 — Automated Testing and Quality Assessment (Phase 13H)

## 1. Test environment and isolation (verified before running)

| Check | Result |
| --- | --- |
| Engine | PostgreSQL 18.1 (x86_64-windows) |
| Development DB | `clipcash_db` (from `.env` `DB_NAME`) |
| Test DB | `test_clipcash_db` (`DB_TEST_NAME` unset → default in [config/settings.py:157](../../config/settings.py#L157)) |
| Runner guard | [config/test_runner.py](../../config/test_runner.py) refuses non-PostgreSQL engines, refuses a test name equal to the app DB or without the `test_` prefix, and after setup asserts `SELECT current_database()` equals the test DB |
| External calls | Bunny HTTP is the only outbound integration; tests patch `apps.videos.providers.bunny._OPENER.open`. Bunny credentials are not set in `.env`. Django's runner forces the locmem email backend. Payment/payout providers are local simulations |
| Conclusion | Isolation established; safe to run |

## 2. Commands executed (all read-only for application data)

```text
venv/Scripts/python.exe --version                         → Python 3.13.14
python -c "import django; print(django.get_version())"    → 5.2.18
python manage.py check                                    → System check identified no issues (0 silenced).
python manage.py makemigrations --check --dry-run         → No changes detected (exit 0)
python manage.py showmigrations                           → 58 [X], 0 [ ]
python -W default manage.py test apps --keepdb -v 2       → see §3
```

`check --deploy` was **not** run against the development `.env` (DEBUG=True makes it meaningless); its behaviour
is covered by `apps/core/tests/test_deployment.py` (e.g. `test_production_configuration_has_no_errors`,
`test_sandbox_payments_fail_the_deploy_check`). No static-analysis tool is installed and installing one was out
of scope.

## 3. Results

| Metric | Value |
| --- | --- |
| Tests discovered / run | **860** |
| Passed | **860** |
| Failures | 0 |
| Errors | 0 |
| Skipped | 0 |
| Expected failures | 0 |
| Duration | 280.7 s reported by the runner (286 s wall clock) |
| Migrations during run | "No migrations to apply" (kept test DB already current) |
| System check during run | 0 issues |
| Python warnings | 1 × `RemovedInDjango60Warning` (URLField `assume_scheme`; from `forms.URLField` for `Organization.website` / `SiteConfiguration.website_url`), 1 × `ResourceWarning` unclosed socket in `videos.tests.test_providers.SigningTests.test_unsafe_ids_and_filenames_are_refused` (mock artefact), 1 × expected `UserWarning` from overriding `DATABASES` in a runner-guard test |
| Log noise | Expected security/job log lines (throttle, CSRF-refusal, retry) produced by negative tests |

### Comparison with the historical baseline

| | Phase 12 baseline | Now | Explanation |
| --- | --- | --- | --- |
| Tests | 779 | 860 | Counting `def test_` in source: **779 at `29e8214`** (Phase 12) and **860 at `HEAD`**; the three later commits added exactly 81 tests and removed none (`git diff 29e8214 HEAD`) |
| Migrations | 58 | 58 | Unchanged; no migrations added after Phase 12 |
| Failures/skips | 0 | 0 | — |

**No regression.**

## 4. Tests by application

| App | Tests | Transactional/concurrency modules | Notes |
| --- | --- | --- | --- |
| accounts | 244 | 2 | Registration, verification, login throttling, reset, admin privilege rules, demo waiver |
| core | 153 | 6 | Features, jobs (incl. concurrency), rate limits, CSP/middleware, deployment config, homepage, dashboard |
| wallets | 78 | 4 | Direct-SQL bypass tests on ledger triggers, wallet zero-money lock, reversal, concurrency |
| videos | 58 | 2 | Bunny signing + mocked HTTP, lifecycle, upload, concurrency |
| memberships | 55 | 2 | Plans, lifecycle, DB access rules, concurrency |
| audit | 54 | 2 | Immutability, purge/export commands, concurrency |
| advertising | 52 | 2 | Organisations, campaigns, web isolation, concurrency |
| viewing | 39 | 2 | Session rules, anti-fraud, web, concurrency |
| payments | 32 | 2 | Reconciliation, callbacks, firewall from ledger, gates, concurrency |
| rewards | 31 | 2 | Policies (four-eyes), evaluation, reversal, concurrency |
| administration | 28 | 0 | Console permissions and operations |
| withdrawals | 24 | 1 (`WithdrawalConcurrencyTests`, 2 tests) | Real lock, demo workflow, web |
| notifications | 12 | 0 | Outbox only |
| reports | 0 | — | No code |
| support | 0 | — | No code |
| **Total** | **860** | 14 modules | |

## 5. Coverage assessment by concern

No line-coverage tool is installed, so this is a qualitative assessment from test names and modules.

| Concern | Coverage | Gaps / proposed regression tests |
| --- | --- | --- |
| Authentication | Strong | Add: login throttling with `DJANGO_TRUSTED_PROXY_COUNT=1` and spoofed `X-Forwarded-For` (CC-004) |
| Authorization | Strong (view + service) | Add: a table-driven test asserting every `/manage/` URL returns 403 for a staff user without permissions |
| Cross-tenant isolation | Good (advertising/payments web tests) | Add: manager of org A cannot see org B's sandbox funding page/intents |
| Business workflows | Per-module good; **no cross-module end-to-end test** from registration to demo reward | Add: one E2E service-level test (register → activate → grant → watch → verify → evaluate → reward visible) |
| Financial constraints | Excellent (direct SQL against triggers) | Add: test proving the role template's revokes hold (CC-005, once applied) |
| Concurrency | Good across 11 apps | Add: concurrent approve vs cancel on demo withdrawals; concurrent reward-policy approvals |
| Idempotency | Good | — |
| Background jobs | Good (leases, retries, stale detection) | — |
| Callback validation | Good (sandbox) | Add: flood of unauthenticated callbacks does not starve authenticated ones (CC-020) |
| Anti-fraud | Rule-level good | Add: scripted real-time heartbeat case documented as accepted risk (CC-014) |
| Failure recovery | Good | — |
| Notifications | Outbox only | Add when workflow notifications are built (CC-009) |
| Browser behaviour | **None** (no Selenium/Playwright; JS untested) | Add: smoke E2E for login, watch session heartbeats (demo `<video>` and Bunny iframe), member nav toggle (CC-015) |
| Accessibility | None automated | Add: axe-core run in browser tests |
| Templates rendering | Indirect via test client | — |

## 6. Quality observations

* Consistent structure (services own authority; views thin; exceptions mapped to 403/404/messages).
* No CI pipeline: tests are run manually (CC-017).
* No linters/type checkers configured; code style is consistent by inspection.
* The `RemovedInDjango60Warning` should be resolved before upgrading to Django 6 (CC-022).
