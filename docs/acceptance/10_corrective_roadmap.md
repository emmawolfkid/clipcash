# 10 — Corrective Roadmap and Acceptance Preparation (Phase 13J)

**Status: proposal for owner review. Nothing in this document has been implemented.**

## 1. Roadmap mapping (no requirements removed or silently renumbered)

| Phase | Original intent | Proposed content after this audit |
| --- | --- | --- |
| 13 | Comprehensive audit | This report set (complete, pending review) |
| **14** | Approved corrective implementation and integration | Batches 14-A … 14-I below |
| 15 | System-wide testing *(original roadmap: "system-wide testing")* | Browser/E2E suite, accessibility scan, load smoke test, full manual acceptance run ([14_manual_acceptance_checklist.md](14_manual_acceptance_checklist.md)) |
| 16 | Deployment hardening *(named in [README.md:187](../../README.md#L187), audit and background-processing docs)* | Role separation (CC-005), monitoring (CC-016), CI/backups (CC-017), HSTS (CC-027), Render blockers B1–B6 |
| 17 | Formal acceptance | Sign-off against the checklist and the issue register |
| Separate programme | Real-money activation (payments incl. AzamPay, real rewards, withdrawals) | Explicitly **out of scope** of Phases 14–17; prerequisites listed in [05_financial_integrity.md §7](05_financial_integrity.md) |

Notifications, Support, Reports and Analytics — original roadmap items — are kept as Phase 14 batches 14-F and
14-G rather than deferred.

## 2. Phase 14 batches

Order follows the requested priority: P0 (none found) → P1 → missing requirements → integration → reliability →
notifications/support → reports → UI/UX → demo preparation → production readiness.

### 14-A — Demo enablement: membership grants and staff provisioning (P1)
* **Objective:** make the member journey completable in the restricted demo without a shell.
* **Issues:** CC-001, CC-012 (demo part), CC-024.
* **Code areas:** `apps/memberships/services.py` (reuse `grant_complimentary`), `apps/administration/views.py` +
  template (member detail "grant complimentary demo membership"), a `provision_demo_staff` management command
  creating permission groups (Reviewer, Finance Operator, Finance Approver) for existing staff accounts.
* **Migrations:** none expected (groups/permissions are data synced by `post_migrate`, as roles already are). If a
  data migration is preferred for groups, it is additive only.
* **Security:** super-admin only; requires `MEMBERSHIPS_COMPLIMENTARY_GRANTS_ENABLED` **and** restricted demo;
  audited; never marks anything paid.
* **Tests:** permission matrix; flag/mode gating; audit event; eligibility rule decided for CC-024.
* **Acceptance:** a super admin grants a demo membership from `/manage/members/<id>/`; the member can start a
  sponsored session; a non-demo deployment refuses.
* **Complexity:** S–M · **Demo impact:** unblocks the member journey.

### 14-B — Hosting decisions and configuration (P1, mostly non-code)
* **Objective:** a usable restricted demo environment.
* **Issues:** CC-002, CC-003, CC-004, CC-006.
* **Work:** confirm Render DB plan/date and upgrade; provision paid worker; choose SMTP provider; configure Bunny
  (or approve `VIDEO_DEMO_ALLOWED` + licensed clip); run the B3 client-IP verification; set `DJANGO_TRUSTED_PROXY_COUNT`.
* **Code (only if B3 fails):** reviewed alternative client-IP resolution with tests.
* **Migrations:** none. **Acceptance:** render.md §14 checklist passes with evidence.
* **Complexity:** S (config) / M (if IP code needed) · **Demo impact:** removes environmental blockers.

### 14-C — Login throttling resilience (P1)
* **Objective:** prevent site-wide sign-in lockout even if IP resolution is imperfect.
* **Issues:** CC-004.
* **Code:** `apps/accounts/throttling.py`, `forms.py` — e.g. per-IP limit counts only failures for non-existent
  accounts, or degrades to a challenge instead of a hard block.
* **Tests:** shared-IP scenario keeps valid users able to sign in; existing throttle tests still pass.
* **Complexity:** S · **Demo impact:** reliability of demo sign-in.

### 14-D — Cross-module integration (P2)
* **Issues:** CC-010 (verified views in advertiser reports), CC-011 (optional sandbox allocation cap for demo
  rewards — product decision first), CC-026 (optional viewing flag).
* **Code:** `apps/advertising/services.py` (`campaign_summary`/`organization_report`), `components/campaign_report.html`,
  `apps/viewing/services.py` (use `verified_views`), optionally `apps/rewards/services.py`.
* **Migrations:** none for CC-010; CC-011 may need none (read allocations at evaluation).
* **Tests:** organisation isolation; revoked sessions excluded; budget cap behaviour if adopted.
* **Complexity:** S–M · **Demo impact:** advertiser story shows results of viewing.

### 14-E — Account self-service (P2)
* **Issues:** CC-013.
* **Code:** `apps/accounts/forms.py`, `views.py`, `urls.py`, templates; email change via existing token/outbox pattern.
* **Migrations:** possibly a pending-email field (additive).
* **Security:** re-authentication for email change; uniform responses; audit events.
* **Complexity:** M · **Demo impact:** low.

### 14-F — Notifications and Support (P2, original roadmap)
* **Issues:** CC-009, CC-008, CC-023 (notifications/support scaffolds).
* **Code:** new email kinds via `register_email_kind`; `Notification` model + member/advertiser inbox; `apps/support`
  models (Ticket, Message), member/advertiser forms, staff queue under `/manage/support/`, permissions, audit.
* **Migrations:** new tables in `notifications` and `support` (additive).
* **Security:** ticket isolation per requester/organisation; staff permission `support.manage_tickets`; no PII in audit metadata.
* **Tests:** isolation, permission, outbox enqueue on each workflow event, idempotency.
* **Complexity:** L · **Demo impact:** medium (shows communications).

### 14-G — Reports and Analytics (P2, original roadmap)
* **Issues:** CC-007, CC-023 (reports scaffold).
* **Code:** `apps/reports` services (read-only aggregates), `/manage/reports/`, advertiser report page, CSV export.
* **Migrations:** none if aggregates are computed on read; optional materialised summaries later.
* **Security:** per-report permissions; advertiser scoping; export audited; demo figures labelled "not money".
* **Complexity:** M–L · **Demo impact:** medium.

### 14-H — Quality and verification (P2/P3)
* **Issues:** CC-015, CC-021, CC-022, CC-020.
* **Work:** opt-in Playwright smoke suite (outside default `manage.py test`); added concurrency tests; resolve
  Django 6 deprecation; per-source callback throttle.
* **Dependencies:** installing Playwright needs owner approval (new dependency).
* **Complexity:** M · **Demo impact:** confidence.

### 14-I — UI/UX and i18n polish (P3)
* **Issues:** CC-030, CC-031, CC-028 (review), CC-018/CC-019 (requirement clarifications — decide, then schedule).
* **Work:** hide Swahili until translated (or translate); translatable JS strings; terminology review.
* **Complexity:** S · **Demo impact:** low.

### Deferred to Phase 16 (production readiness)
CC-005 (role separation + purge restriction), CC-016 (monitoring), CC-017 (CI, backups, restore drill),
CC-027 (HSTS), staff MFA (CC-012), CC-014 anti-fraud hardening plan, CC-029 ledger cross-entry rules (real-money
design input).

## 3. Recommended first batch

**14-A (demo membership grants + staff provisioning) together with the configuration decisions in 14-B.**
These two unblock the end-to-end member demonstration with the smallest code change and no migration, and they do
not touch any financial lock.

## 4. Acceptance preparation

* Use [14_manual_acceptance_checklist.md](14_manual_acceptance_checklist.md) as the Phase 15/17 script.
* Re-run `manage.py test apps --keepdb` after every batch; the count must only increase from 860.
* Every batch must leave all `DirectSqlBypassTests`, `ZeroMoneyTests`, `RealWithdrawalTests` and
  `FeatureFlagTests` passing unchanged.
