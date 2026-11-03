# 09 — Prioritized Issue Register (Phase 13I)

Severity: **P0** critical (financial-integrity violation / exploitable vulnerability) · **P1** high · **P2** medium ·
**P3** low. Confidence: **Confirmed** (code evidence) · **Suspected** (plausible, not demonstrated) ·
**Unverified** (depends on an environment the audit could not access).

**No P0 issue was found.** Intentionally disabled features are listed separately in §3 and are **not** defects.

## 1. Summary

| ID | Sev | Module | Title | Demo blocker | Prod blocker | Confidence |
| --- | --- | --- | --- | --- | --- | --- |
| CC-001 | P1 | memberships / payments | No in-product path to an ACTIVE membership | **Yes** | Yes | Confirmed |
| CC-002 | P1 | videos / viewing | No playable video source in the restricted demo configuration; placeholder cannot verify views | **Yes** | Yes | Confirmed (config) |
| CC-003 | P1 | core.jobs / ops | Background worker (paid) and SMTP not provisioned; emails and scheduled processing stall | **Yes** (conditional) | Yes | Confirmed (documented B1/B2) |
| CC-004 | P1 | accounts | Client-IP resolution behind Render proxy unverified → site-wide login/registration throttling | **Yes** (conditional) | Yes | Confirmed in code; impact unverified |
| CC-005 | P1 | database / audit / ledger | Single DB owner role: all trigger-based guarantees bypassable; audit purge GUC unrestricted | No | **Yes** | Confirmed |
| CC-006 | P1 | ops | Render DB plan/expiry unverified (Free: 30-day expiry, no backups) | **Yes** (if Free) | Yes | Unverified |
| CC-007 | P2 | reports | Reports & Analytics module not implemented | No | Yes | Confirmed |
| CC-008 | P2 | support | Support module not implemented; UI tells users to "contact support" | No | Yes | Confirmed |
| CC-009 | P2 | notifications | Only 4 account emails; no workflow or in-app notifications | No | Yes | Confirmed |
| CC-010 | P2 | advertising / viewing | Advertiser report shows "not available yet" despite available verified-view data | No (cosmetic in demo) | Yes | Confirmed |
| CC-011 | P2 | rewards / payments | Demo rewards not linked to (sandbox) advertiser funding | No | No (real-money phase) | Confirmed |
| CC-012 | P2 | accounts / administration | Limited role model; SoD flows need several manually-permissioned staff; no staff MFA | **Partial** (demo prep) | Yes | Confirmed |
| CC-013 | P2 | accounts | No profile editing, email change or account closure | No | Yes | Confirmed |
| CC-014 | P2 | viewing | Anti-fraud relies on client-reported heartbeats; no device/IP/multi-account signals | No | Yes (before real money) | Suspected |
| CC-015 | P2 | QA | No browser/E2E tests; Bunny player event bridge unverified | No | Yes | Confirmed (gap) |
| CC-016 | P2 | ops | No error monitoring, alerting or off-host log retention | No | Yes | Confirmed |
| CC-017 | P2 | ops | No CI; backup/restore not automated or rehearsed | No | Yes | Confirmed |
| CC-018 | P2 | advertising / videos | Advertisers cannot upload their own creatives (staff upload + grant) | No | Clarify | Confirmed |
| CC-019 | P2 | advertising | Campaign targeting limited to membership plans | No | Clarify | Confirmed |
| CC-029 | P2 | wallets (ledger) | Funding separation is DB-enforced per entry only; multi-entry routing relies on app registry | No | Yes (real-money phase) | Suspected |
| CC-020 | P3 | payments / withdrawals | Sandbox callbacks: unauthenticated attempts persisted; one global throttle bucket | No | No | Confirmed |
| CC-021 | P3 | QA | Thin tests: notifications outbox-only, withdrawals 2 concurrency tests, reports/support none | No | No | Confirmed |
| CC-022 | P3 | advertising / core | `RemovedInDjango60Warning` (URLField scheme) + test ResourceWarning | No | No | Confirmed |
| CC-023 | P3 | reports / support / notifications | Boilerplate scaffold files (dead code) | No | No | Confirmed |
| CC-024 | P3 | memberships | `grant_complimentary` skips email/status checks, contradicting an access comment | No | No | Confirmed |
| CC-025 | P3 | videos | Playback tokens not viewer-bound; HLS protection depends on Bunny CDN token auth | No | Verify | Unverified |
| CC-026 | P3 | viewing | Viewing gated by `advertisers` flag (no own flag) | No | No | Confirmed |
| CC-027 | P3 | config | HSTS 0 by default until HTTPS verified | No | Yes (enable) | Confirmed |
| CC-028 | P3 | product | Brand/terminology legal review ("ClipCash", "payout") | No | Review | Advisory |
| CC-030 | P3 | i18n | Swahili selectable but no translations exist | No (if hidden) | Yes for sw market | Confirmed |
| CC-031 | P3 | i18n | Watch-page JS status strings hard-coded English | No | No | Confirmed |

Counts: **P0 0 · P1 6 · P2 14 · P3 11** (31 issues).

## 2. Detailed findings

### CC-001 — No in-product path to an ACTIVE membership (P1)
* **Module:** memberships, payments
* **Description:** Members can choose a plan (PENDING), but nothing in the web product activates it.
* **Expected:** The demo member journey continues from membership selection to sponsored viewing.
* **Actual:** Paid activation requires `live_payments` (locked) and a registered payment verifier (none exist)
  ([apps/memberships/services.py:402-438](../../apps/memberships/services.py#L402-L438)); sandbox settlement only
  writes `MembershipActivationPreview` ([apps/payments/services.py:488-512](../../apps/payments/services.py#L488-L512));
  the only activation path is the `grant_complimentary_membership` CLI, refused unless
  `DJANGO_MEMBERSHIPS_COMPLIMENTARY_GRANTS=True` ([services.py:450-483](../../apps/memberships/services.py#L450-L483)).
  Sponsored viewing requires an active membership ([apps/advertising/services.py:856-858](../../apps/advertising/services.py#L856-L858)).
* **Verification:** source trace; `MembershipTests.test_settled_membership_payment_is_only_previewed`.
* **Impact:** core member journey cannot be demonstrated without an operator shell (paid-only on Render).
* **Proposed correction:** a Super-Administrator console action for **demo complimentary grants** (reusing
  `grant_complimentary`, audited, restricted-demo only, clearly labelled "complimentary — no payment"), plus a
  documented demo-provisioning command. Do **not** activate memberships from sandbox payments.
* **Regression tests:** grant via console requires super admin + flag + restricted demo; audit event; member gains
  playback; non-demo environments refuse.
* **Dependencies:** business approval for complimentary demo memberships; CC-012 (staff provisioning).
* **Demo blocker:** Yes · **Production blocker:** Yes (until real payments) · **Confidence:** Confirmed

### CC-002 — No playable video source in the restricted demo (P1)
* **Module:** videos, viewing
* **Expected:** Members can watch a sponsored video and produce a verified view.
* **Actual:** Outside DEBUG the provider is Bunny; credentials are unset (Render B5). The demo provider requires
  `VIDEO_DEMO_ALLOWED` (documented as never for public deployments) and, without `VIDEO_DEMO_SAMPLE_URL`, renders a
  placeholder that states the session "cannot be verified" ([templates/viewing/session.html:13](../../templates/viewing/session.html#L13),
  [apps/videos/providers/demo.py:39-42](../../apps/videos/providers/demo.py#L39-L42)). No licensed sample clip is in the repository.
* **Impact:** sponsored viewing, verification and demo rewards cannot be shown.
* **Proposed correction:** configuration decision — either configure Bunny Stream (library, API key, CDN host,
  token key, CDN token auth on) or, for the restricted demo only, approve `VIDEO_DEMO_ALLOWED` with an owned/licensed
  clip; record videos' duration. No code change strictly required.
* **Regression tests:** existing provider tests; add a browser smoke test (CC-015).
* **Dependencies:** Bunny account or licensed clip. **Demo blocker:** Yes · **Prod:** Yes · **Confidence:** Confirmed

### CC-003 — Background worker and SMTP not provisioned (P1)
* **Module:** core.jobs, notifications, ops
* **Actual:** Registration/verification/reset emails, scheduled campaign activation (future starts), automatic
  reward evaluation, session expiry, membership expiry and reconciliation jobs all depend on `run_jobs`; Render
  Background Workers are paid-only and SMTP is blocked from free instances ([docs/deployment/render.md §8-9, B1-B2](../deployment/render.md)).
* **Impact:** without the worker, nobody can activate an account by email; demo rewards appear only if staff press
  "evaluate"; open sessions are not expired.
* **Proposed correction:** provision the paid worker and an SMTP provider; or, for a staff-driven demo, pre-provision
  demo accounts (admin-created ACTIVE accounts + documented waiver) and accept manual evaluation.
* **Tests:** existing job tests; operational check of `/manage/health/` heartbeat.
* **Demo blocker:** Yes unless the staff-driven alternative is accepted · **Prod:** Yes · **Confidence:** Confirmed

### CC-004 — Client IP behind proxy unverified; site-wide throttling (P1)
* **Module:** accounts (throttling), audit
* **Actual:** `ACCOUNTS_TRUSTED_PROXY_COUNT` defaults to 0 ([config/settings.py:261](../../config/settings.py#L261)), so behind
  Render every client shares the proxy address. `LoginForm.clean` refuses **before** authentication when
  `LOGIN_FAILURES_PER_IP` (50/15 min) is reached ([apps/accounts/forms.py:52-58](../../apps/accounts/forms.py#L52-L58));
  registration (10/h) and email requests (20/h) are also per-IP. Audit records the proxy IP.
* **Reproduction (staging):** with count 0 behind a proxy, submit 50 failed logins for any address, then attempt a
  valid login from another client → throttled.
* **Impact:** trivial availability attack on sign-in for every user; loss of forensic IP data.
* **Proposed correction:** verify Render's `X-Forwarded-For` behaviour (render.md B3 procedure) and set the count;
  if Render passes client-supplied values, implement a reviewed alternative (e.g. platform-specific header) with tests.
  Consider making the per-IP login limit apply only to *unknown* accounts or raising it with a CAPTCHA fallback.
* **Tests:** spoofed XFF ignored; per-IP limit isolates clients when count = 1.
* **Demo blocker:** Conditional (Yes if hosted on Render with count 0) · **Prod:** Yes · **Confidence:** Confirmed (code), Unverified (platform)

### CC-005 — Single database owner role (P1)
* **Module:** database, ledger, wallets, withdrawals, audit
* **Actual:** All financial and audit guarantees are triggers; each migration states the owner/superuser can disable
  them. The audit purge flag `clipcash.audit_purge` is a custom GUC any role can `SET LOCAL`; TRUNCATE is not blocked
  ([apps/audit/migrations/0002_append_only_trigger.py](../../apps/audit/migrations/0002_append_only_trigger.py)).
  Role template exists but is not applied (Render B6).
* **Impact:** any compromise of the app credential (or an operator mistake) can rewrite ledger/audit history.
* **Proposed correction:** apply owner/runtime role separation; restrict the purge path to the owner role (e.g.
  `SECURITY DEFINER` purge function or trigger checking `current_user`); revoke TRUNCATE; re-run tests under the
  runtime role. Consider audit hash chaining.
* **Tests:** runtime role cannot UPDATE/DELETE/TRUNCATE ledger/audit, cannot purge; migrations still apply as owner.
* **Demo blocker:** No · **Prod:** Yes · **Confidence:** Confirmed

### CC-006 — Render database plan and expiry unverified (P1)
* **Actual:** Free Render Postgres expires after 30 days (+14-day grace), no backups ([render.md §10, B4](../deployment/render.md)).
  Account state not accessible to the audit.
* **Correction:** confirm plan/creation date; upgrade or schedule the demo inside the window; take a `pg_dump`.
* **Demo blocker:** Yes if Free and near expiry · **Prod:** Yes · **Confidence:** Unverified

### CC-007 — Reports & Analytics not implemented (P2)
* **Evidence:** `apps/reports/*` is `startapp` boilerplate; no URLs/models; README lists reports as not implemented.
  Existing reporting = admin overview counts, advertiser status counts, `audit_export` CLI.
* **Correction:** define report catalogue (campaign performance, viewing funnel, membership KPIs, demo reward
  totals, operational queues), date filters, CSV export with permissions and audit events.
* **Tests:** permission per report; organisation isolation for advertiser reports; export audit event.
* **Demo:** No (admin overview suffices) · **Prod:** Yes · **Confidence:** Confirmed

### CC-008 — Support not implemented (P2)
* **Evidence:** `apps/support/*` boilerplate; "Please contact support" in [apps/advertising/services.py:701](../../apps/advertising/services.py#L701); only optional footer contact.
* **Correction:** minimum viable support: contact/ticket form for members and advertisers, staff queue with
  statuses, audit, notification hooks; FAQ/help pages.
* **Demo:** No · **Prod:** Yes · **Confidence:** Confirmed

### CC-009 — Notifications limited (P2)
* **Evidence:** only `ACTIVATION`, `CONFIRM_EMAIL`, `ALREADY_REGISTERED`, `PASSWORD_RESET` are enqueued
  ([apps/accounts/services.py:58-106](../../apps/accounts/services.py#L58-L106), [forms.py:121](../../apps/accounts/forms.py#L121));
  `apps/notifications/views.py` is empty.
* **Missing:** advertiser application decision, team invitation, campaign approval/rejection/pause, membership
  expiring/expired, viewing review outcome, demo withdrawal status, support replies; in-app notification list; preferences.
* **Correction:** register additional email kinds through the existing outbox; add an in-app `Notification` model + list.
* **Demo:** No · **Prod:** Yes · **Confidence:** Confirmed

### CC-010 — Advertiser report not connected to verified viewing (P2)
* **Evidence:** `templates/components/campaign_report.html` hard-codes "Verified views / Unique viewers / Spend: not
  available yet"; `apps.viewing.services.verified_views()` exists ([apps/viewing/services.py:658-662](../../apps/viewing/services.py#L658-L662)) with no caller.
* **Correction:** show verified views and unique viewers per campaign (organisation-scoped); keep "Spend" as N/A.
* **Tests:** counts only own organisation's campaigns; excludes revoked/rejected sessions.
* **Demo:** No · **Prod:** Yes · **Confidence:** Confirmed

### CC-011 — Demo rewards not linked to advertiser funding (P2)
* **Evidence:** `evaluate_session` consumes `Campaign.demo_budget_amount` ([apps/rewards/services.py:299-312](../../apps/rewards/services.py#L299-L312));
  `SandboxCampaignAllocation` is not referenced by rewards.
* **Impact:** the demo cannot illustrate business rule 10 (rewards funded by authenticated, reconciled advertiser funds).
* **Correction (optional, sandbox only):** when sandbox is active, cap demo reward consumption by active sandbox
  allocations; otherwise keep the labelled demo budget. Requires product decision.
* **Demo:** No · **Prod:** No (real-money design will replace it) · **Confidence:** Confirmed

### CC-012 — Role model and separation-of-duties provisioning (P2)
* **Evidence:** roles limited to SUPER_ADMIN/CONTENT_MANAGER/MEMBER ([apps/accounts/roles.py](../../apps/accounts/roles.py));
  finance/review permissions (`payments.*`, `rewards.*`, `withdrawals.*`, `viewing.review_viewing`, advertising review)
  must be granted manually by a superuser; four-eyes rules need 2–3 distinct privileged staff; no staff MFA.
* **Correction:** define staff groups (Reviewer, Finance Operator, Finance Approver, Support Agent, Auditor) synced
  like existing role groups; a demo-provisioning command; staff MFA (TOTP) before production.
* **Demo:** Partial (manual provisioning needed) · **Prod:** Yes · **Confidence:** Confirmed

### CC-013 — Profile management gaps (P2)
* **Evidence:** `ProfileView` is read-only ([apps/accounts/views.py:288-301](../../apps/accounts/views.py#L288-L301)).
* **Correction:** edit name/phone; verified email change (re-verification); account closure request; data export.
* **Demo:** No · **Prod:** Yes · **Confidence:** Confirmed

### CC-014 — Anti-fraud limits (P2)
* **Evidence:** credit derives from client positions bounded by server elapsed time ([apps/viewing/services.py:346-406](../../apps/viewing/services.py#L346-L406));
  flags are per-session behaviour; no device fingerprint, IP reputation, cross-account or velocity-across-accounts signals.
* **Impact:** a scripted client sending plausible real-time heartbeats can generate verified views (bounded by
  per-member limits). Acceptable for demo rewards; not for real money.
* **Correction:** documented accepted risk now; later: IP/device clustering, CAPTCHA/attention checks, statistical
  anomaly review, Bunny-side view analytics reconciliation.
* **Demo:** No · **Prod:** Yes before real money · **Confidence:** Suspected

### CC-015 — No browser/E2E tests (P2)
* **Evidence:** no Selenium/Playwright; JS (`video-watch.js`, `video-upload.js`, `member-nav.js`) untested; Bunny
  `player.js` postMessage bridge never exercised.
* **Correction:** add a small Playwright suite (opt-in, not in the default run) for login, demo `<video>` session to
  verification, member nav, accessibility scan.
* **Demo:** No (manual checklist covers it) · **Prod:** Yes · **Confidence:** Confirmed

### CC-016 — No error monitoring / alerting (P2)
* **Correction:** error tracker with PII scrubbing; alert on stale worker heartbeat, job backlog, 5xx rate, health 503.
* **Demo:** No · **Prod:** Yes · **Confidence:** Confirmed

### CC-017 — No CI; backups not rehearsed (P2)
* **Correction:** CI running `check`, `makemigrations --check`, full PostgreSQL suite; scheduled encrypted backups;
  restore drill documented with evidence.
* **Demo:** No · **Prod:** Yes · **Confidence:** Confirmed

### CC-018 — Advertiser creative upload (P2, requirement clarification)
* **Evidence:** uploads only in `/manage/videos/` (staff); advertisers pick from staff-granted videos.
* **Correction:** confirm whether advertisers must upload creatives; if yes, add an organisation-scoped upload with
  staff moderation before grant.
* **Demo:** No · **Prod:** Depends on decision · **Confidence:** Confirmed

### CC-019 — Campaign targeting (P2, requirement clarification)
* **Evidence:** only `target_plans` ([apps/advertising/services.py:859-861](../../apps/advertising/services.py#L859-L861)).
* **Correction:** confirm required dimensions (region, language, age band, schedule); implement only approved ones.
* **Demo:** No · **Prod:** Depends · **Confidence:** Confirmed

### CC-029 — Ledger flow-of-funds separation across entries (P2)
* **Evidence:** `ledger_verify_entry` checks classes **within one entry**; `ADVERTISER_UNVERIFIED` inflows are not
  restricted by the DB ([apps/wallets/chart.py:142-150](../../apps/wallets/chart.py#L142-L150)), so separation across
  entries via neutral accounts (`PAYMENT_CLEARING`, `CASH`) relies on the posting-type registry.
* **Impact:** none today (all postings locked); a design prerequisite for real money.
* **Correction (real-money phase):** DB rule that `ADVERTISER_FUNDING_PENDING` credits must reference a reconciled
  external payment record; reconciliation reports per funds domain.
* **Demo:** No · **Prod:** Yes for real money · **Confidence:** Suspected

### P3 issues (condensed)

| ID | Evidence | Proposed correction | Tests |
| --- | --- | --- | --- |
| CC-020 | [apps/payments/services.py:302-327](../../apps/payments/services.py#L302-L327) (global `_throttle` key; `ProviderEvent` created for unauthenticated attempts); same pattern in withdrawals payout callback | Throttle per source and only persist a counter (or sample) for unauthenticated attempts; return 404 when sandbox off (already) | Forged flood does not block signed notice |
| CC-021 | `apps/notifications/tests/test_outbox.py` only; `WithdrawalConcurrencyTests` 2 tests; reports/support none | Add tests listed in [08_test_coverage.md](08_test_coverage.md) | — |
| CC-022 | Test log `RemovedInDjango60Warning` (URLField `assume_scheme`) for `Organization.website` ([apps/advertising/models.py:42](../../apps/advertising/models.py#L42)), `SiteConfiguration.website_url` ([apps/core/models.py:67](../../apps/core/models.py#L67)); unclosed-socket ResourceWarning in a provider test | Set `FORMS_URLFIELD_ASSUME_HTTPS=True` or `assume_scheme="https"` on form fields; close mock sockets | Run tests with `-W error::DeprecationWarning` |
| CC-023 | `apps/reports/*`, `apps/support/*`, `apps/notifications/views.py` boilerplate | Replace with real modules (CC-007/008/009) or remove scaffolds | — |
| CC-024 | `grant_complimentary` ([apps/memberships/services.py:450-483](../../apps/memberships/services.py#L450-L483)) never calls `member_eligibility`; comment at [apps/memberships/access.py:48-49](../../apps/memberships/access.py#L48-L49) says otherwise | Decide intended rule; either check eligibility (status ACTIVE at least) or correct the comment | Grant to SUSPENDED/PENDING account refused |
| CC-025 | Embed token has no viewer binding ([apps/videos/providers/bunny.py](../../apps/videos/providers/bunny.py) docstring) | Keep short TTL; enable Bunny CDN token auth + allowed referrers; verify in Bunny dashboard | Manual check |
| CC-026 | [apps/viewing/views.py:25](../../apps/viewing/views.py#L25) | Optional dedicated `viewing` flag | Flag off → 404 |
| CC-027 | [config/settings.py:55](../../config/settings.py#L55) | Follow render.md §12 once HTTPS verified | `check --deploy` W004 cleared |
| CC-028 | Brand/terminology | Legal/marketing review before public launch | — |
| CC-030 | `locale/` has only `.gitkeep`; `sw` offered in switcher | Hide `sw` until a reviewed catalogue exists, or produce translations | Switcher lists only available catalogues |
| CC-031 | [static/js/video-watch.js:41-50](../../static/js/video-watch.js#L41-L50) | Pass translated strings via `data-` attributes | — |

## 3. Intentionally disabled or deferred (not defects)

| Item | Mechanism | Activation requires |
| --- | --- | --- |
| Live payments (incl. AzamPay) | `live_payments` locked; no provider adapter; `PAYMENTS_MODE` has no live value | Separate reviewed real-money phase with legal/provider approval |
| Real-money rewards | `real_money_rewards` locked; campaign funding constrained `UNFUNDED` | Same |
| Real withdrawals / payouts | `withdrawals` locked; `withdrawals_withdrawalrequest` trigger; wallet posting trigger | Same, plus reviewed migrations replacing the locks |
| Membership activation from payments | Preview only | Real payments |
| Public registration on demo | `RESTRICTED_DEMO_MODE` | Reviewed public launch |

## 4. Environment verification requirements (not code defects)

Render plan/expiry (CC-006), worker/SMTP (CC-003), client IP (CC-004), Bunny configuration (CC-002, CC-025),
HSTS (CC-027), role separation applied on the hosted DB (CC-005).
