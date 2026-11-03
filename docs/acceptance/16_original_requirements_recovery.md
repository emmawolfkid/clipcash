# 16 — Original Requirements Recovery (Phase 13 Addendum)

**Date:** 2026-11-03 · **Baseline:** `main` @ `a87c81b` · **Type:** read-only recovery; nothing implemented.

This document records **what can be proven** about the original requirements for the later roadmap phases, where
each statement comes from, and what is already built. It does **not** turn recommendations into requirements:
anything without a source is labelled *Proposed enhancement* or *Business decision pending*.

## 1. Sources searched

| Source | Location | Result |
| --- | --- | --- |
| Requirements folder | `docs/requirements/` | **Empty** (only `.gitkeep`, created in Phase 00B) |
| Security folder | `docs/security/` | **Empty** (only `.gitkeep`) |
| Architecture decision records | `docs/architecture/decisions/0001-ledger-and-background-processing.md` | Ledger and jobs only |
| Architecture docs (15 files) | `docs/architecture/*.md` | Several "known limitations" / future notes (cited below) |
| README | `README.md` | Feature table incl. "not implemented" row; Phase 16 reference |
| Deployment guide | `docs/deployment/render.md` | Blockers B1–B7 |
| Git history (21 commits) | `git log`, `git log -S`, `git grep <commit>` | Historical "Coming soon" placeholders; app scaffolds |
| Owner instructions | Phase 13 master prompt and this addendum (supplied in the conversation) | Phase titles 13–17 and the business model |
| Phase 13 audit | `docs/acceptance/01`–`14` | Implementation evidence and issue IDs (CC-xxx) |

**Unavailable evidence:** no original roadmap document, product specification, user stories, wireframes or
acceptance criteria for Notifications, Support, Reports, Analytics, testing, deployment or launch exist in the
repository or its history. The **phase titles below come from the project owner's instructions** and are recorded
as such.

## 2. Source codes used in this document

| Code | Meaning |
| --- | --- |
| **OS-1** | Owner statement: this addendum, "Context" (original Phases 13–17 titles) |
| **OS-2** | Owner statement: Phase 13 master prompt §1 "Business model" item 13 ("Administrators manage users, videos, advertising, memberships, security, finance-related demo operations, reports and support") |
| **OS-3** | Owner statement: Phase 13 master prompt §13B requirement list (Notifications, Support, Reports and analytics, Audit logging, Background processing, Deployment operations) |
| **OS-4** | Owner statement: Phase 13 master prompt §13J ("Preserve the original roadmap requirements, including Notifications, Support, Reports, Analytics, system-wide testing, deployment readiness and formal acceptance") |
| **OS-5** | Owner statement: Phase 13 master prompt §2 phase table (Phases 00–12 as implemented) and §1 business-model items 1–13 |
| **RD-1** | `apps/notifications`, `apps/support`, `apps/reports` created and registered in `INSTALLED_APPS` in Phase 00B (`fedb14e`) |
| **RD-2** | Admin navigation item **"Reports — Coming soon"** present from Phase 04 (`c94e453`) through Phase 11 (`1805406`) in `templates/administration/layout.html`; removed in Phase 12 (`29e8214`) with the note "Reports, the old 'Coming soon' item, is now off the roadmap list for Phase 12" ([docs/architecture/administration-dashboard.md:136](../architecture/administration-dashboard.md)) |
| **RD-3** | [README.md:73](../../README.md#L73): "Live payments, real rewards, real withdrawals, reports, support — Not implemented" |
| **RD-4** | "Deployment hardening is completed in Phase 16" — [README.md:187](../../README.md#L187), present since Phase 01B (`18aa13d`); also [docs/architecture/audit.md:106](../architecture/audit.md) (role separation "recommended for Phase 16"), [background-processing.md:159](../architecture/background-processing.md) ("alert wiring is a deployment task (Phase 16)"), [scripts/database/production_roles.example.sql:4](../../scripts/database/production_roles.example.sql) |
| **RD-5** | [docs/architecture/memberships.md:152](../architecture/memberships.md): "No expiry reminder emails yet (the outbox supports adding them)" |
| **RD-6** | [docs/architecture/background-processing.md:157](../architecture/background-processing.md): "Email bodies are plain text; no bounce or complaint processing yet (needs the email provider's webhooks)" |
| **RD-7** | Site configuration "company and support contacts" (Phase 02A, `f3dd7e3`): `SiteConfiguration.support_email` / `support_phone` ([apps/core/models.py:72-74](../../apps/core/models.py#L72-L74), [docs/architecture/core.md:13](../architecture/core.md)) |
| **RD-8** | [docs/architecture/administration-dashboard.md](../architecture/administration-dashboard.md) "Browser acceptance requirements (after Phase 12)" — six manual browser checks (roles, widths 360/768/1280 px, keyboard, screen reader + contrast, every review workflow, real worker + email on staging) |
| **RD-9** | [docs/architecture/advertising.md:112-114](../architecture/advertising.md) "Reporting": `organization_report` / `campaign_summary` (counts per status, demo-budget totals) |
| **RD-10** | `docs/deployment/render.md` §13 blockers B1–B7 and §14 first-deployment checklist |

Classification vocabulary (as requested): **Explicitly documented original requirement** · **Existing implemented
behaviour** · **Proposed enhancement** · **Business decision pending** · **Intentionally deferred**.

## 3. Recovered original phase requirements (13–17)

| Orig. phase | Requirement (as stated) | Source | Classification | Detail available? |
| --- | --- | --- | --- | --- |
| 13 | Notifications and Customer Support | OS-1; corroborated by OS-3, OS-4, RD-1, RD-3, RD-7 | **Explicitly documented original requirement** (owner-stated; repository corroborates the domain, not the scope) | **No.** No functional detail exists beyond the domain names |
| 14 | Reports, Auditing and Analytics | OS-1; corroborated by OS-2, OS-3, OS-4, RD-1, RD-2, RD-3, RD-9 | **Explicitly documented original requirement** (owner-stated; "Reports" was a planned admin menu item for 8 phases) | **No** report catalogue exists |
| 15 | System-Wide Testing and Security Hardening | OS-1, OS-4; corroborated by RD-8 | **Explicitly documented original requirement** | Partial: RD-8 lists six browser checks |
| 16 | Deployment and Operational Readiness | OS-1, OS-4; **independently documented** by RD-4 and RD-10 | **Explicitly documented original requirement** | Yes: role separation, alert wiring, render.md checklist and blockers |
| 17 | Acceptance and Controlled Launch | OS-1, OS-4 | **Explicitly documented original requirement** | **No** acceptance criteria existed before `14_manual_acceptance_checklist.md` |

> **Correction to report 10:** [10_corrective_roadmap.md §1](10_corrective_roadmap.md) described Phase 13's original intent
> as "Comprehensive audit". Per OS-1, the original Phase 13 was **Notifications and Customer Support**; the audit
> replaced its *slot*, not its requirements. Report 10 is left unchanged as instructed; its phase plan is
> superseded by [15_roadmap_reconciliation.md](15_roadmap_reconciliation.md).

## 4. Notifications — verified implementation and requirement status

### 4.1 What exists (evidence)

| Capability | Where | Evidence | Tests |
| --- | --- | --- | --- |
| Durable email outbox model | `notifications.OutboundEmail` (status PENDING/SENT/FAILED/EXPIRED/SKIPPED, attempts, error class, expiry, `user_id`) | [apps/notifications/models.py](../../apps/notifications/models.py), migration `0001_initial` | `test_outbox.py` (12 tests) |
| Enqueue in caller's transaction | `enqueue_email(kind, recipient, user, context)` with context-key whitelist and `base_url` validation | [apps/notifications/services.py:64-103](../../apps/notifications/services.py#L64-L103) | yes |
| Delivery job, retries, failure handling | `notifications.deliver_email` (6 attempts, 60 s→1 h backoff, permanent vs retryable SMTP errors, `on_failure` → FAILED + audit `notifications.email.failed`) | [services.py:106-163](../../apps/notifications/services.py#L106-L163) | yes |
| Expiry / duplicate prevention | Per-email expiry; job idempotency key `email:<id>`; status check makes re-runs no-ops | same | yes |
| Retention | `notifications.prune_outbox` (90 days, non-pending) | [services.py:166-170](../../apps/notifications/services.py#L166-L170) | yes |
| Admin delivery visibility | Read-only `OutboundEmailAdmin` with **masked recipient**, no bodies/tokens | [apps/notifications/admin.py](../../apps/notifications/admin.py) | via operations admin tests |
| Health visibility | `/manage/health/` email check (failed in window, waiting too long) | [apps/administration/summary.py:269-285](../../apps/administration/summary.py#L269-L285) | `test_dashboard.py` |
| Email kinds (4) | `ACTIVATION`, `CONFIRM_EMAIL`, `ALREADY_REGISTERED`, `PASSWORD_RESET` | [apps/accounts/emails.py](../../apps/accounts/emails.py), [accounts/services.py:58-106](../../apps/accounts/services.py#L58-L106), [accounts/forms.py:121](../../apps/accounts/forms.py#L121) | `test_registration.py`, `test_password_reset.py` |
| In-app *surfacing* (not notifications) | Advertiser home lists pending invitations; rejected campaign shows "Changes requested: …"; member **activity feed** summarises own memberships, sessions, rewards, wallet, withdrawals | `templates/advertising/home.html:10-17`, `templates/advertising/campaign_detail.html:6`, [apps/core/dashboard.py:132+](../../apps/core/dashboard.py#L132) | `test_member_experience.py` |
| Event hooks usable by notifications | Django signals `videos.playback_authorized`, `viewing.session_reversed`; every business action emits an audit event | [apps/videos/services.py:45](../../apps/videos/services.py#L45), [apps/viewing/services.py:56](../../apps/viewing/services.py#L56) | — |
| URLs / views / templates / API | **None** — `apps/notifications/views.py` is a scaffold | — | — |

### 4.2 Capability classification

| Capability | Status now | Classification | Notes |
| --- | --- | --- | --- |
| Account email notifications | Implemented (4 kinds) | Existing implemented behaviour | Delivery depends on worker + SMTP (CC-003) |
| Membership lifecycle emails (expiring, expired, activated, suspended) | Missing | **Documented gap** for expiry reminders (RD-5); other events: Proposed enhancement | — |
| Advertiser & campaign events (application decision, invitation, campaign approved/rejected/paused) | Missing (partially surfaced in-app) | Proposed enhancement under owner requirement "Notifications" (OS-1) | Invitation email especially relevant: invitees are not told today |
| Viewing / anti-fraud outcomes to members | Missing | Proposed enhancement; **business decision pending** (telling a member they were flagged can aid evasion) | — |
| Simulated reward notifications | Missing | Proposed enhancement; must say "simulated, not money" | Financial invariant wording |
| Demo withdrawal notifications | Missing | Proposed enhancement; demo-labelled | — |
| Support ticket events | Missing (no support module) | Depends on Support (OS-1) | — |
| In-app inbox, read/unread | Missing | Proposed enhancement | No source specifies an inbox |
| Notification preferences / opt-out | Missing | **Business decision pending** (transactional vs optional notices) | — |
| Delivery templates | Plain-text builders per kind | Existing; HTML email is a Proposed enhancement | RD-6 |
| Bounce / complaint handling | Missing | **Documented gap** (RD-6) | Needs provider choice (B2) |
| Background processing, retries, failures | Implemented | Existing implemented behaviour | — |
| Administrative delivery visibility | Implemented (admin + health) | Existing implemented behaviour | — |
| Authorization / privacy / duplicate prevention | Implemented for outbox (masked recipient, no tokens stored, idempotent job) | Existing | In-app inbox will need per-recipient scoping |
| SMS / push channels | Not referenced anywhere | **Not a recovered requirement** — do not assume | Decision pending if wanted |

**Mandatory from original evidence:** that a Notifications capability exists beyond account email (OS-1, OS-3, OS-4)
and that membership expiry reminders are a known gap (RD-5). **Everything else needs owner approval of scope.**

## 5. Customer Support — verified implementation and requirement status

### 5.1 What exists

| Item | Evidence |
| --- | --- |
| `apps/support` | `startapp` boilerplate only: empty `models.py`, `views.py`, `admin.py`, `tests.py`; no migrations, URLs or templates (RD-1, RD-3) |
| Support contacts | `SiteConfiguration.support_email` / `support_phone` shown in the footer when set ([templates/base.html](../../templates/base.html) footer; RD-7) |
| References to support in UI | "This campaign was paused by ClipCash staff. Please contact support." ([apps/advertising/services.py:701](../../apps/advertising/services.py#L701)) |
| Staff permissions for support | None defined; no support role (CC-012) |
| Elsewhere in the code base | No ticket, case, enquiry, contact-form or message model anywhere (`grep` across `apps/`) |

### 5.2 Capability classification

| Capability | Classification |
| --- | --- |
| Member support requests | Proposed enhancement under owner requirement "Customer Support" (OS-1) |
| Advertiser support requests | Proposed enhancement (OS-1); visibility within an organisation is a **decision** |
| Ticket categories | **Business decision pending** (list not specified) |
| Ticket creation / status transitions / history | Proposed enhancement |
| Staff assignment and queues | Proposed enhancement (OS-2: administrators manage support) |
| Staff/member replies, internal notes | Proposed enhancement; internal-note policy is a **decision** |
| Attachments | **Not specified** — business decision pending (no media storage exists today; would need object storage, scanning, size/type policy) |
| Notifications on ticket events | Depends on Notifications (15-A) |
| Closure and reopening | Proposed; **reopening window/rules pending** |
| Permissions and tenant isolation | Required by existing platform security standards (organisation isolation pattern in advertising) |
| Support reporting | Part of Reports (OS-1 Phase 14) |
| Service-level targets | **Business decision pending** |

## 6. Reports, Auditing and Analytics — verified implementation and requirement status

### 6.1 What already exists outside `apps/reports`

| Report-like capability | Location | Scope / access | Category of figures |
| --- | --- | --- | --- |
| Executive overview (members, memberships, advertising, videos, viewing outcomes, rewards, payments, wallets, withdrawals, jobs) | [apps/administration/summary.py:65-212](../../apps/administration/summary.py#L65-L212) | Staff; each section gated by its permission | Counts; demo/sandbox kept in separate sections |
| Operational health | [summary.py:215-305](../../apps/administration/summary.py#L215-L305) | Staff | Status only |
| Security & audit summary (24 h) | `summary.security_summary` → `/manage/security/` | `can_view_audit` | Counts of events |
| Member directory with activity | `summary.member_directory`, `/manage/members/` | `accounts.view_user` or super admin | Per member |
| Advertiser report | `advertising.organization_report` / `campaign_summary` ([services.py:896-911](../../apps/advertising/services.py#L896-L911)) | Own organisation (+ staff) | Status counts, **demo budget (not money)**; verified views shown as "not available yet" (CC-010) |
| Simulated reward totals | `rewards.campaign_totals`, `budget_summary`, `member_totals` ([apps/rewards/services.py:414-444](../../apps/rewards/services.py#L414-L444)) | Staff console; member history | **Simulated** |
| Sandbox journal balances | `payments.sandbox_balances()` ([apps/payments/services.py:178-190](../../apps/payments/services.py#L178-L190)), `funds_for(org)` | Staff / owning advertiser | **Sandbox** |
| Wallet reconciliation findings | `wallets.wallet_services.reconcile`, `/manage/wallets/reconciliation/` | `wallets.view_wallets` | **Real ledger** (all zero) |
| Withdrawal queues | `withdrawals.staff_queue` | `withdrawals.view_*` | **Demo** |
| Verified views per campaign | `viewing.verified_views(campaign)` — **unused** | — | Evidence counts |
| Audit export | `manage.py audit_export` (JSON Lines) | CLI; audited | Audit |
| Job status | `manage.py jobs_status` (JSON) | CLI | Operations |
| Audit trail itself ("Auditing") | `apps/audit` (append-only events, catalogue, admin view, purge/export) | `can_view_audit` | — |

None of these offers **date-range filtering, trends over time or data export in the UI**; all are current-state
counts.

### 6.2 Capability classification

| Capability | Status | Classification |
| --- | --- | --- |
| Member registration & activity | Current counts only | Proposed enhancement (date ranges, trends) under OS-1 Phase 14 |
| Membership plan performance | Status counts only | Proposed enhancement |
| Advertiser organisation activity | Status counts | Proposed enhancement |
| Campaign performance | Status + demo budget; verified views not wired | Gap (CC-010) + Proposed enhancement |
| Verified / rejected / suspicious views | Overview outcome counts | Proposed enhancement (per campaign/date) |
| Simulated reward issuance & reversal | `campaign_totals` | Existing (staff); Proposed: date filters, export |
| Sandbox payments & reconciliation | Overview counts, exceptions queue | Existing; Proposed: period summaries |
| Demo wallet & withdrawal statistics | Overview counts | Existing; Proposed |
| Administrative operational reports | Health, queues | Existing |
| Audit & security statistics | 24 h security summary; audit export CLI | Existing (Auditing); Proposed: longer windows |
| Filtering and date ranges | None | Proposed enhancement |
| Role-based access | Pattern exists per section | Required (existing standard) |
| Data export | Audit CLI only | **Business decision pending** (formats, PII) |
| "Analytics" (trends, funnels, cohorts, dashboards) | None | **Business decision pending — no definition exists** |
| Query performance / accuracy | Aggregates are single-query today | Must be verified with `EXPLAIN` on realistic volumes |

## 7. Other recovered requirements (Phases 15–17)

| Requirement | Source | Status |
| --- | --- | --- |
| Six browser acceptance checks after Phase 12 | RD-8 | Not executed (CC-015); folded into Phase 16 testing |
| Database role separation | RD-4 (audit.md:106, roles SQL) | Template only (CC-005) |
| Alert wiring for worker heartbeat | RD-4 (background-processing.md:159) | Not done (CC-016) |
| Render blockers B1–B7 and first-deployment checklist | RD-10 | Open (CC-002–CC-006) |
| HSTS enablement after HTTPS verified | render.md §12 | Pending (CC-027) |
| Formal acceptance / controlled launch | OS-1 | Checklist drafted (report 14); no launch criteria were previously documented |

## 8. Intentionally deferred (unchanged)

Live payments (AzamPay or other), real-money rewards, real withdrawals/payouts, real campaign funding, membership
activation from payments — locked in code and PostgreSQL (see [05_financial_integrity.md](05_financial_integrity.md)).
These are not part of the original Phases 13–17 as stated by the owner and remain a separate reviewed programme.
