# 15 — Roadmap Reconciliation (Phase 13 Addendum)

**Status:** proposal for owner approval. Nothing here is implemented. This document **supersedes the phase plan in
[10_corrective_roadmap.md](10_corrective_roadmap.md)** (left unchanged as instructed); report 10's batch content is
reused, renumbered as shown in §5.

Sources and source codes (OS-x = owner statements, RD-x = repository evidence) are defined in
[16_original_requirements_recovery.md §2](16_original_requirements_recovery.md). Issue IDs (CC-xxx) are from
[09_prioritized_issue_register.md](09_prioritized_issue_register.md).

## 1. Key reconciliation facts

1. The original Phase 13 (**Notifications and Customer Support**) and Phase 14 (**Reports, Auditing and Analytics**)
   were not cancelled. Their slots were used for the audit and its corrections; their requirements move to Phase 15.
2. **No detailed specification for Phases 13–17 exists** in the repository. Only the domains are corroborated
   (app scaffolds since Phase 00B, a "Reports — Coming soon" menu item in Phases 04–11, README "not implemented",
   "Phase 16 deployment hardening" since Phase 01B). Detailed scope therefore needs owner approval (§7).
3. Report 10 described the original Phase 13 as "Comprehensive audit" — **incorrect** per OS-1; corrected here.
4. Original Phases 15–17 shift by one (to 16–18) because of the inserted corrective Phase 14. Requirement content
   is unchanged.

## 2. Phase mapping

| Original phase (OS-1) | New phase(s) | Reason |
| --- | --- | --- |
| 13 Notifications and Customer Support | **15-A** Notifications, **15-B** Support | Needs the hosting fixes (worker/SMTP) and the complete member journey first |
| *(inserted)* Comprehensive audit | 13 (done) | Owner decision |
| *(inserted)* Corrective implementation | **14-A, 14-B, 14-C** | P1 findings block the demo and core journey |
| 14 Reports, Auditing and Analytics | **15-C** Reports (incl. auditing statistics), **15-D** Analytics | Reports reuse 14-C integration (verified views); Analytics scope undefined |
| *(gap found by audit)* Account self-service | **15-E** | P2 finding CC-013 had no slot in the provisional sequence; email change depends on 15-A |
| 15 System-Wide Testing and Security Hardening | **16** | — |
| 16 Deployment and Operational Readiness | **17** | — |
| 17 Acceptance and Controlled Launch | **18** | — |

## 3. Roadmap reconciliation matrix

### 3.1 Phases 00–12 (implemented phases; residual issues carried forward)

| Orig. phase | Req ID | Requirement | Source | Current implementation | Audit evidence | Missing / residual | Pri | Deps | Corrective phase | Acceptance criteria |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 00 | ORQ-00 | Django/PostgreSQL foundation | OS-5; `fedb14e` | Implemented | 01 §1; 08 (runner guard) | No CI (CC-017) | P2 | — | 17 | CI runs check, migration check, full suite |
| 01 | ORQ-01 | Authentication and accounts | OS-5; `a116e16`, `18aa13d` | Implemented, verified (244 tests) | 04 §1 | Proxy-IP throttling (CC-004); profile self-service (CC-013); staff MFA (CC-012) | P1/P2 | 14-B | 14-B, 15-E, 16 | See batches |
| 02A | ORQ-02A | Core configuration | OS-5; `f3dd7e3` | Implemented | 01 §3 | Swahili offered but untranslated (CC-030) | P3 | — | 16 | Switcher shows only languages with a catalogue |
| 02B | ORQ-02B | Audit trail | OS-5; `0f596c6` | Implemented, verified | 04 §5, 05 | DB guarantee needs role separation (CC-005) | P1 | 14-B | 14-B (design+staging), 17 (production) | Runtime role cannot update/delete/truncate/purge audit |
| 02C | ORQ-02C | Double-entry ledger | OS-5; `5bf7e51` | Implemented, locked | 05 | Cross-entry funds separation app-only (CC-029) | P2 | real-money programme | Deferred design input | Documented; not in Phases 14–18 |
| 02D | ORQ-02D | Background jobs | OS-5; `30dc930` | Implemented | 07 | Hosted worker (CC-003); alerting (CC-016) | P1/P2 | hosting budget | 14-B, 17 | Fresh heartbeat; alert on stale heartbeat |
| 03 | ORQ-03 | Membership management | OS-5; `5e9e317` | Implemented; **no in-product activation** | 03 §1 | CC-001, CC-024 | P1 | — | **14-A** | See 14-A |
| 04 | ORQ-04 | Video management and Bunny Stream | OS-5; `c94e453` | Implemented; Bunny mocked only | 02 R19–R21 | CC-002, CC-025; creative upload decision (CC-018) | P1 | Bunny account | 14-B (config), 14-C | Live Bunny upload → READY → playback |
| 05 | ORQ-05 | Advertising and campaigns | OS-5; `dea7963` | Implemented | 03 §2 | Reports gap (CC-010); targeting decision (CC-019) | P2 | 14-C | 14-C, decision | Advertiser sees verified views |
| 06 | ORQ-06 | Verified viewing and anti-fraud | OS-5; `0d6d14e` | Implemented (server rules) | 03, 04 | Browser path unverified (CC-015); heuristic limits (CC-014); own flag (CC-026) | P2 | 14-C | 14-C, 16 | Browser session reaches verified outcome |
| 07 | ORQ-07 | Simulated rewards engine | OS-5; `6a30afd` | Implemented (demo) | 05 | Not linked to sandbox funding (CC-011, decision) | P2 | decision | 14-C (if approved) | Per decision |
| 08 | ORQ-08 | Sandbox payments and reconciliation | OS-5; `9e198c2` | Implemented (sandbox) | 05 | Callback flood handling (CC-020) | P3 | — | 16 | Forged flood cannot starve signed notices |
| 09 | ORQ-09 | Member wallets | OS-5; `01c8d9a` | Implemented, TZS 0 lock | 05 | — | — | — | — | Invariants 6–7 tests unchanged |
| 10 | ORQ-10 | Demo withdrawals and payouts | OS-5; `0fa52f3` | Implemented (demo) | 05 | Thin concurrency tests (CC-021) | P3 | — | 16 | Approve/cancel race tests |
| 11 | ORQ-11 | Integrated member dashboard | OS-5; `1805406` | Implemented | 06 | JS strings not translatable (CC-031) | P3 | — | 16 | Translated status text |
| 12 | ORQ-12 | Administration and operations centre | OS-5; `29e8214` | Implemented | 03 §3 | Role model/SoD provisioning (CC-012); RD-8 browser checks not executed (CC-015) | P2 | — | 14-A (provisioning), 16 | RD-8 checks pass |

### 3.2 Original Phases 13–17

| Orig. phase | Req ID | Requirement | Source | Current implementation | Audit evidence | Missing functionality | Pri | Deps | Corrective phase | Acceptance criteria |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 13 | ORQ-13.1 | Notifications (beyond account email) | OS-1, OS-3, OS-4 | Outbox + 4 account emails; read-only admin; health check | 16 §4.1; 02 R42 | Workflow emails, in-app inbox, read/unread; preferences (decision) | P2 (CC-009) | 14-B (worker/SMTP) | **15-A** | §6 batch 15-A |
| 13 | ORQ-13.2 | Membership expiry reminders | RD-5 | None | memberships.md:152 | Reminder emails | P2 (CC-009) | 15-A | **15-A** | Reminder sent once per subscription per window |
| 13 | ORQ-13.3 | Bounce / complaint processing | RD-6 | None | background-processing.md:157 | Provider webhooks | P3 | SMTP provider choice | 15-A (if provider supports) or 17 | Bounced address stops further sends |
| 13 | ORQ-13.4 | Customer Support | OS-1, OS-2, OS-3, OS-4 | Footer contacts only (RD-7); `apps/support` empty | 16 §5 | Ticketing, queues, replies, history | P2 (CC-008) | 15-A, CC-012 roles | **15-B** | §6 batch 15-B |
| 13 | ORQ-13.5 | "Contact support" path for paused campaigns | code: advertising/services.py:701 | Message only | 06 §4 | Working support channel | P2 (CC-008) | 15-B | **15-B** | Message links to a ticket form |
| 14 | ORQ-14.1 | Reports | OS-1, OS-2, OS-3, OS-4; RD-2, RD-3, RD-9 | Overview/health/security summaries; advertiser status report; reward totals; sandbox balances | 16 §6.1 | Report catalogue, date ranges, exports (decision), campaign performance | P2 (CC-007, CC-010) | 14-C | **15-C** | §6 batch 15-C |
| 14 | ORQ-14.2 | Auditing | OS-1 | `apps/audit` complete; 24 h security summary; `audit_export` CLI | 04 §5 | Longer-window audit/security statistics; production immutability (CC-005) | P1/P2 | 14-B, 17 | 15-C (statistics), 17 (roles) | Auditor report over chosen period; runtime role cannot alter audit |
| 14 | ORQ-14.3 | Analytics | OS-1, OS-4 | None | 16 §6.2 | **Undefined** — trends/funnels/cohorts need a definition | P2 | 15-C | **15-D** | Defined after owner decision |
| — | ORQ-AS | Account self-service (profile, email change, closure) | Audit finding CC-013 (not an original phase item) | Read-only profile | 02 R06 | Edit profile, verified email change, closure request | P2 | 15-A | **15-E** | Email change requires re-verification and is audited |
| 15 | ORQ-15.1 | System-wide testing | OS-1, OS-4; RD-8 | 860 unit/integration tests; no browser tests | 08 | E2E/browser, accessibility, cross-module E2E, load smoke | P2 (CC-015, CC-021) | 14–15 complete | **16** | §6 batch 16 |
| 15 | ORQ-15.2 | Security hardening | OS-1 | Strong baseline | 04 | Staff MFA, throttle resilience, callback flood, anti-fraud accepted-risk statement, dependency/secret scans | P1/P2 (CC-004, CC-012, CC-014, CC-020) | — | **14-B** (CC-004), **16** (rest) | §6 |
| 16 | ORQ-16.1 | Deployment and operational readiness | OS-1; RD-4, RD-10 | Scripts + guide; nothing deployed and verified | 07 | Role separation, monitoring/alerting, CI, backups + restore drill, HSTS, DB plan | P1/P2 (CC-005, CC-006, CC-016, CC-017, CC-027) | 16 | **17** (14-B does environment decisions) | render.md §14 passes with evidence |
| 17 | ORQ-17.1 | Acceptance and controlled launch | OS-1, OS-4 | Checklist drafted (report 14) | 11, 12, 14 | Executed checklist, sign-off, launch criteria; legal/brand review (CC-028) | P2 | 17 | **18** | Signed checklist; zero open P1; owner-approved P2 waivers |

Every confirmed P1 and P2 issue appears above or in §4; no original requirement is omitted.

## 4. Issue-to-phase allocation (completeness check)

| Phase | Issues |
| --- | --- |
| 14-A | CC-001, CC-024, CC-012 (staff permission provisioning part) |
| 14-B | CC-003, CC-004, CC-006, CC-005 (design + staging verification), CC-002 (configuration decision) |
| 14-C | CC-002 (integration), CC-010, CC-025 (verify), CC-026, CC-011 (if approved), CC-015 (watch-path smoke only) |
| 15-A | CC-009, CC-023 (notifications scaffold), CC-031 (strings reused in notices) |
| 15-B | CC-008, CC-023 (support scaffold) |
| 15-C | CC-007, CC-023 (reports scaffold) |
| 15-D | (Analytics — no audit issue; requirement ORQ-14.3) |
| 15-E | CC-013 |
| 16 | CC-012 (MFA, staff roles), CC-014, CC-015, CC-020, CC-021, CC-022, CC-030, CC-031 (if not in 15-A), CC-018/CC-019 (only if approved) |
| 17 | CC-005 (production), CC-016, CC-017, CC-027, CC-006 (final) |
| 18 | CC-028, sign-off |
| Separate real-money programme | CC-029; all intentionally locked features |

**Moved earlier than the provisional sequence, with reasons:**
* **CC-004 throttle resilience → 14-B** (security; an attacker could block every sign-in on the demo host).
* **CC-005 role-separation design and staging verification → 14-B** (technical dependency: new tables in 15-A/B/C
  need correct grants and append-only treatment from the start; production application stays in 17).
* **CC-012 staff permission provisioning → 14-A** (four-eyes reward policies and demo withdrawals cannot be shown
  without several distinct privileged staff; support queues in 15-B also need a staff group).
* **CC-010 verified views in advertiser report → 14-C** (part of a complete journey; prerequisite for 15-C campaign reports).

## 5. Corrected execution phases and dependencies

```
14-A Demo membership activation + staff provisioning ──┐
14-B Hosting & security corrections (config + CC-004) ─┼─► 14-C Video integration & complete member journey
                                                        │            │
                                                        ▼            ▼
                                   15-A Notifications ──► 15-B Support ──┐
                                   15-A ──► 15-E Account self-service    │
                                   14-C ──► 15-C Reports ──► 15-D Analytics
                                                                          ▼
                                               16 System-wide testing & security hardening
                                                                          ▼
                                               17 Deployment & operational readiness
                                                                          ▼
                                               18 Client acceptance & controlled launch
```

14-A and 14-B can run in parallel (14-B is mostly configuration). 15-C can start after 14-C, in parallel with
15-A/15-B; its support-ticket report section waits for 15-B.

Mapping from report 10 batches: 10's 14-A → 14-A; 14-B + 14-C → 14-B; 14-D → 14-C; 14-E → 15-E; 14-F → 15-A + 15-B;
14-G → 15-C; 14-H → 16; 14-I → 16; "Phase 16 deferred items" → 17.

## 6. Batch acceptance criteria

All batches share these **standing requirements**:

* Preserve all 15 financial invariants ([05](05_financial_integrity.md)); no real-money activation; no change to
  existing triggers, constraints or migrations (new behaviour only through **new forward** migrations).
* `DirectSqlBypassTests`, `ZeroMoneyTests`, `RealWithdrawalTests`, `FeatureFlagTests` pass unchanged; total test
  count only increases from 860; `makemigrations --check` clean; `check --deploy` clean for the documented Render
  configuration.
* Every state change goes through a service with server-side authorisation and an audit event in the same transaction.
* Organisation/member isolation: other parties' objects return 404.
* Any figure shown must stay in its category — (1) real ledger amounts (all zero/locked), (2) sandbox payment
  records, (3) simulated rewards, (4) demo withdrawals, (5) advertiser demo budgets — and **never be summed across
  categories** or labelled revenue, earnings, balance or payable.

### 14-A — Secure complimentary demo membership activation
| Field | Content |
| --- | --- |
| Functional scope | Super-Administrator console action on `/manage/members/<id>/` to grant a complimentary membership (plan choice, labelled "complimentary — no payment"); decision on CC-024 eligibility rule; `provision_demo_staff` command / staff groups (Reviewer, Finance Operator, Finance Approver, later Support Agent) synced like role groups |
| Reuse | `memberships.services.grant_complimentary`, `_activate`, audit events; `accounts.roles.sync_role_groups` pattern; `administration` member detail view |
| Dependencies | Owner approval of demo complimentary policy (§7 D1) |
| Security | Super admin only; requires `MEMBERSHIPS_COMPLIMENTARY_GRANTS_ENABLED` **and** `RESTRICTED_DEMO_MODE`; CSRF POST; reason required; audited; amount paid 0; never touches payments or ledger |
| Data model | None expected (groups/permissions are data via `post_migrate`) |
| Migrations | None (if a custom permission is added, an additive model-Meta permission migration) |
| PostgreSQL tests | Permission matrix (super admin / staff / member); gating by both settings; audit event; subscription ACTIVE with source COMPLIMENTARY and amount 0; ineligible member rule; group sync idempotent |
| Integration tests | Grant → `check_access` ok → `start_session` allowed |
| Manual scenarios | Checklist A10–A13, C2, C6–C7 |
| Definition of done | Demo member reaches a sponsored session without shell access; outside restricted demo the action is absent and the service refuses |
| Owner decisions | D1 (grant policy, duration, who may receive), D2 (staff groups) |

### 14-B — Hosting and security corrections
| Field | Content |
| --- | --- |
| Functional scope | Confirm DB plan/date and upgrade (CC-006); paid worker (CC-003); SMTP provider (B2); video source decision (CC-002); client-IP verification and `DJANGO_TRUSTED_PROXY_COUNT` (CC-004); login throttle resilience so a shared IP cannot lock out valid users; role-separation design (new audit migration restricting purge to the owner role, TRUNCATE revoked) verified on a staging copy (CC-005) |
| Reuse | `scripts/database/production_roles.example.sql`; render.md §13–14 procedures; `throttling.Throttle` |
| Dependencies | Hosting budget (D3), email provider (D4) |
| Security | Spoofed `X-Forwarded-For` ignored; per-IP limit cannot block known-good accounts; purge flag ineffective for runtime role |
| Data model | None for throttling; audit purge restriction via **new** audit migration (replacing function body, not editing 0002) |
| Migrations | 0–1 (audit) |
| PostgreSQL tests | Throttle shared-IP scenario; XFF spoofing; full suite run **as the runtime role** on staging; runtime role cannot UPDATE/DELETE/TRUNCATE ledger/audit or purge |
| Integration tests | Activation email delivered end-to-end on staging via worker |
| Manual scenarios | render.md §14 steps 1–12; checklist A3, A5, A6, C12 |
| Definition of done | render.md §14 checklist evidenced; B1–B4, B6 closed or formally accepted |
| Owner decisions | D3, D4, D5 (video source) |

### 14-C — Video integration and complete member journey
| Field | Content |
| --- | --- |
| Functional scope | Configure Bunny (CDN token auth on) or approved licensed demo clip; verify `player.js` heartbeat bridge; wire `viewing.verified_views` into advertiser and staff campaign reports (verified views, unique viewers; "spend" stays N/A); optional `viewing` feature flag (CC-026); CC-011 only if approved |
| Reuse | `videos.providers.bunny`, `video-watch.js`, `advertising.campaign_summary`, `viewing.verified_views`, `rewards.campaign_totals` |
| Dependencies | 14-A, 14-B; D5, D6 (CC-011) |
| Security | Counts organisation-scoped; unique-viewer counts never expose member identities; small-number suppression decision (D12) |
| Data model / migrations | None expected |
| PostgreSQL tests | Report counts exclude revoked/rejected/invalidated sessions; isolation; flag gating |
| Integration tests | Service-level E2E: register → activate → grant (14-A) → start session → heartbeats → verified → policy approved → evaluate → reward visible → wallet still 0 |
| Manual scenarios | A12–A18, B10, C3, C5–C8 in a real browser (desktop + mobile) |
| Definition of done | The full member and advertiser demo journeys run in a browser on the demo host |
| Owner decisions | D5, D6, D12 |

### 15-A — Notifications
| Field | Content |
| --- | --- |
| Functional scope | New email kinds (owner-approved list from §7 D7) e.g. advertiser decision, invitation, campaign decision/pause, membership activated/expiring/expired (RD-5), demo withdrawal status, support events; in-app notification list with read/unread; optional preferences (D8); bounce handling if provider supports (RD-6) |
| Reuse | `register_email_kind`, `enqueue_email` (same-transaction), `deliver_email` retries/failure hook, `OutboundEmailAdmin`, health email check, existing audit events as trigger points |
| Dependencies | 14-B (worker + SMTP) |
| Security | Notifications enqueued inside the business transaction (no notice for rolled-back actions); recipient-scoped inbox (404 for others); no tokens or financial detail beyond labelled demo/simulated wording; fraud-review outcomes per D9; masked recipients in admin |
| Data model | New `notifications.Notification` (recipient, kind, params, link, `read_at`, `dedupe_key`, unique `(recipient, dedupe_key)`); optional `NotificationPreference` |
| Migrations | Additive `notifications` migration(s) |
| PostgreSQL tests | Each event enqueues exactly once (dedupe, retries, concurrent actions); rollback enqueues nothing; inbox isolation; mark-read idempotent; templates contain "simulated"/"demo" wording where relevant; no notification for locked financial features |
| Integration tests | Campaign approval → email outbox row + inbox entry; expiry job → reminder once |
| Manual scenarios | B3, B4, B8 (notices received), A11 (activation notice) |
| Definition of done | Every approved event notifies by approved channel; failed deliveries visible in admin/health |
| Owner decisions | D7, D8, D9, D10 (email language) |

### 15-B — Customer Support
| Field | Content |
| --- | --- |
| Functional scope | Ticket creation for members and advertisers (category per D11), statuses (e.g. OPEN → IN_PROGRESS → WAITING_ON_REQUESTER → RESOLVED → CLOSED, reopen per D13), staff queue under `/manage/support/`, assignment, public replies and internal notes, history, notifications via 15-A; attachments only if D14 approves |
| Reuse | Organisation isolation pattern (`advertising.organization_for`), staff permission pattern (`_staff_with`), audit events, DB state-machine trigger pattern, 15-A notifications, `SiteConfiguration` support contacts |
| Dependencies | 15-A; 14-A staff groups |
| Security | Requester sees own tickets (advertiser visibility per D15); staff need `support.*` permissions; internal notes never shown to requesters; support staff do not gain wallet/finance access implicitly; no deletes (trigger) |
| Data model | `support.Ticket`, `support.TicketMessage` (author, body, `internal`), optional `TicketAttachment`; status-transition and no-delete triggers |
| Migrations | Additive `support` migrations incl. a trigger migration |
| PostgreSQL tests | Isolation (member/advertiser/other org), permission matrix, valid/invalid transitions at DB level, reopen rule, concurrency on assignment, notification enqueued once |
| Integration tests | "Contact support" from a staff-paused campaign opens a prefilled ticket |
| Manual scenarios | New: member ticket → staff reply → member reply → resolve → reopen (if allowed) → close |
| Definition of done | All CC-008 references resolved; support queue usable by a Support Agent without superuser rights |
| Owner decisions | D11, D13, D14, D15, D16 (SLA targets) |

### 15-C — Reports (incl. auditing statistics)
| Field | Content |
| --- | --- |
| Functional scope | Approved report catalogue (D17) with date ranges in the site time zone: members & memberships; advertisers & campaigns (verified/rejected/suspicious views); simulated rewards issued/reversed; sandbox payments & reconciliation exceptions; demo withdrawals; operations (jobs, outbox); audit & security statistics; support (after 15-B); CSV export only if D18 approves |
| Reuse | `administration.summary`, `advertising.campaign_summary`, `viewing.verified_views`, `rewards.campaign_totals`/`budget_summary`, `payments.sandbox_balances`, `wallets.reconcile` findings, `withdrawals.staff_queue`, `audit_export` logic — **no parallel ledger, no recomputed balances** |
| Dependencies | 14-C; 15-B for support section |
| Security | Per-report permission; advertiser reports scoped to own organisation; exports audited (new audit event, possibly a new audit category migration as in earlier phases); PII minimised in exports |
| Data model | None preferred (read-time aggregates); indexes only if `EXPLAIN ANALYZE` on seeded volumes shows need |
| Migrations | Possibly index-only and audit-category migrations |
| PostgreSQL tests | Figures equal the authoritative service outputs on fixtures; five money-like categories never combined (test asserts separate keys/labels); date-boundary tests; isolation; export permission + audit; query count bounds |
| Integration tests | Report totals reconcile with overview counts |
| Manual scenarios | Staff and advertiser each open their reports; verify labels "real (locked, 0)", "sandbox", "simulated", "demo", "demo budget (not money)" |
| Definition of done | Catalogue delivered; accuracy tests pass; menu item "Reports" restored for permitted staff |
| Owner decisions | D17, D18, D12 |

### 15-D — Analytics
| Field | Content |
| --- | --- |
| Functional scope | **To be defined (D19).** Candidate scope, not approved: time-series trends and viewing funnel (sessions → verified → rewarded) built on 15-C services |
| Reuse | 15-C services |
| Dependencies | 15-C; D19 |
| Security | Aggregates only; no third-party analytics scripts without a privacy decision (CSP currently forbids them) |
| Data model / migrations | Possibly summary tables populated by a job — only if performance requires |
| Tests | Same accuracy and category-separation rules as 15-C |
| Definition of done | Owner-approved analytics views delivered |
| Owner decisions | D19 |

### 15-E — Account self-service
| Field | Content |
| --- | --- |
| Functional scope | Edit name/phone; email change with re-verification (old address informed); account-closure request (handled by staff; data retention per policy) |
| Reuse | Token generators, outbox, `validate_access_change` rules, audit |
| Dependencies | 15-A |
| Security | Re-authentication for email change; uniform responses; email change invalidates tokens/sessions as today's token hash design does |
| Migrations | Possibly a pending-email field (additive) |
| Tests | Enumeration-safe responses; token reuse refused; audit events |
| Definition of done | CC-013 closed |
| Owner decisions | D20 (closure/retention policy) |

### 16 — System-wide testing and security hardening (original Phase 15)
| Field | Content |
| --- | --- |
| Functional scope | Opt-in browser E2E suite (RD-8 checks), accessibility scan, cross-module E2E, added concurrency tests (CC-021), Django 6 deprecation (CC-022), callback flood handling (CC-020), staff MFA and final staff roles (CC-012), anti-fraud accepted-risk statement + hardening backlog (CC-014), Swahili decision (CC-030), JS i18n (CC-031), dependency and secret scanning |
| Dependencies | 14–15 complete; approval to add test/scanning dependencies (D21) |
| Definition of done | RD-8's six browser checks and report 14 sections A–D pass on staging; no open P1; P2s fixed or waived |

### 17 — Deployment and operational readiness (original Phase 16)
| Field | Content |
| --- | --- |
| Functional scope | Apply role separation in production (CC-005); monitoring and alerting incl. worker heartbeat (CC-016, RD-4); CI and scheduled encrypted backups + restore drill (CC-017); HSTS (CC-027); final DB plan (CC-006) |
| Definition of done | render.md §14 complete with evidence; restore drill recorded; alerts tested |

### 18 — Client acceptance and controlled launch (original Phase 17)
| Field | Content |
| --- | --- |
| Functional scope | Execute report 14 with the client; legal/brand review (CC-028); controlled-launch criteria (D22) |
| Definition of done | Signed acceptance; real-money features still locked |

## 7. Open business decisions

| ID | Decision | Needed by |
| --- | --- | --- |
| D1 | Complimentary demo memberships: who may grant, to whom, plan, duration, labelling | 14-A |
| D2 | Staff groups and which permissions each holds | 14-A |
| D3 | Hosting budget: paid worker, paid web service, paid database | 14-B |
| D4 | Email provider (and bounce-webhook support) | 14-B / 15-A |
| D5 | Video source for demo: Bunny account vs licensed sample clip with `VIDEO_DEMO_ALLOWED` | 14-B / 14-C |
| D6 | Should demo rewards be capped by sandbox advertiser allocations (CC-011)? | 14-C |
| D7 | Which events notify, and by which channel (email / in-app) | 15-A |
| D8 | Notification preferences/opt-out for non-essential notices | 15-A |
| D9 | Whether members are told about fraud-review outcomes, and in what words | 15-A |
| D10 | Email language(s) (English only, or Swahili too) | 15-A |
| D11 | Support ticket categories | 15-B |
| D12 | Minimum group size for viewer counts shown to advertisers (privacy) | 14-C / 15-C |
| D13 | Reopening rules (window, who may reopen) | 15-B |
| D14 | Attachments: allowed? types, size, storage, malware scanning, retention | 15-B |
| D15 | Advertiser ticket visibility: creator only or whole organisation (owners vs managers) | 15-B |
| D16 | Service-level targets and whether they are shown to requesters | 15-B |
| D17 | Report catalogue and audience per report | 15-C |
| D18 | Data export: allowed? formats, PII rules, who may export | 15-C |
| D19 | Definition of "Analytics" | 15-D |
| D20 | Account closure and data-retention policy | 15-E |
| D21 | Approval to add test/scan dependencies (e.g. Playwright, pip-audit) | 16 |
| D22 | Controlled-launch criteria (audience, duration, rollback triggers) | 18 |
| D23 | Advertiser creative upload (CC-018) and targeting dimensions (CC-019) | before 16 if wanted |

## 8. Recommended first Phase 14 batch

**14-A**, started together with the configuration decisions of **14-B** (D3–D5). 14-A is small, needs no schema
change, touches no financial lock, and removes the main obstacle to demonstrating the member journey; 14-B's
decisions are on the critical path for everything from 14-C onward.
