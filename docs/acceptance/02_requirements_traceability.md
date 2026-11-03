# 02 — Requirements Traceability (Phase 13B)

Each row: **Requirement → Implementation → Evidence → Tests → Status → Gap → Priority**.

Status vocabulary: **Verified** (source inspected + passing tests exercise it) · **Implemented, unverified**
(source present, tests partial or mocked only) · **Partial** · **Missing** · **Intentionally disabled** ·
**Environment-dependent**. All 860 tests passed on 2026-11-03 (see [08_test_coverage.md](08_test_coverage.md)).

## A. Accounts and identity

| # | Requirement | Implementation | Evidence | Tests | Status | Gap | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R01 | Registration | `RegisterView` + `services.register` (email only; password set at activation) | [apps/accounts/views.py:66-87](../../apps/accounts/views.py#L66-L87), [forms.py:29-39](../../apps/accounts/forms.py#L29-L39) | `test_registration.py` | Verified | Closed in restricted demo by design (`registration` is `demo_restricted`) | — |
| R02 | Email verification | Activation link (PENDING→ACTIVE on password set) and confirm-email (POST-only confirm) with 24 h tokens bound to account state | [views.py:96-151](../../apps/accounts/views.py#L96-L151), [tokens.py](../../apps/accounts/tokens.py) | `test_verification.py` | Verified | Delivery requires the job worker + SMTP (CC-003) | — |
| R03 | Demo email-verification waiver | Listed `DEMO_ACCOUNTS`, restricted demo on, no payments possible | [access.py:13-52](../../apps/accounts/access.py#L13-L52) | `test_demo_verification.py`, `test_demo_workflows.py` | Verified | — | — |
| R04 | Login / logout | Throttled `LoginForm`; POST-only logout | [forms.py:42-90](../../apps/accounts/forms.py#L42-L90), [views.py:192-213](../../apps/accounts/views.py#L192-L213) | `test_login.py`, `test_throttling.py` | Verified | Per-IP throttle becomes site-wide behind an unconfigured proxy (CC-004) | P1 |
| R05 | Password reset | Outbox email, 1 h token, ACTIVE accounts only, uniform responses | [forms.py:101-122](../../apps/accounts/forms.py#L101-L122), [views.py:222-262](../../apps/accounts/views.py#L222-L262) | `test_password_reset.py` | Verified | — | — |
| R06 | Account / profile management | Read-only profile + password change | [views.py:288-301](../../apps/accounts/views.py#L288-L301), `templates/accounts/profile.html` | `test_access.py` | **Partial** | No edit of name/phone, no email change, no account closure/data export | P2 (CC-013) |
| R07 | Staff account administration | Django admin with privilege rules; Group admin superuser-only | [apps/accounts/admin.py](../../apps/accounts/admin.py), [services.py:178+](../../apps/accounts/services.py#L178) | `test_admin_security.py` | Verified | Only 3 roles; no finance/support/reviewer roles | P2 (CC-012) |

## B. Memberships

| # | Requirement | Implementation | Evidence | Tests | Status | Gap | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R08 | Membership plans (versioned) | `MembershipPlan` + versions, staff management via Django admin | [apps/memberships/services.py:150-228](../../apps/memberships/services.py#L150-L228) | `test_plans.py` | Verified | — | — |
| R09 | Membership selection | `choose_plan` → PENDING subscription | [services.py:274-300](../../apps/memberships/services.py#L274-L300) | `test_lifecycle.py`, `test_web.py` | Verified | — | — |
| R10 | Membership activation | Paid: `activate_with_verified_payment` (requires locked `live_payments`); free: `grant_complimentary` (CLI, flag off) | [services.py:402-483](../../apps/memberships/services.py#L402-L483) | `test_lifecycle.py` | **Intentionally disabled (paid) / CLI-only (free)** | **No in-product path to an ACTIVE membership**; blocks sponsored viewing | **P1 (CC-001)** |
| R11 | Membership eligibility | `check_access` reads DB each time; suspended/expired deny immediately | [apps/memberships/access.py:41-60](../../apps/memberships/access.py#L41-L60) | `test_database_and_access.py` | Verified | — | — |
| R12 | Membership payments | Sandbox intents + reconciliation, **preview only** | [apps/payments/services.py:233-255, 488-512](../../apps/payments/services.py#L233-L255) | `apps/payments/tests` | Intentionally disabled (production); sandbox verified | Real activation needs separate financial review | Deferred |
| R13 | Expiry / renewal processing | `process_expiry` job; renewal window | [services.py:547+](../../apps/memberships/services.py#L547) | `test_lifecycle.py` | Verified | Requires worker (CC-003) | — |

## C. Advertisers and campaigns

| # | Requirement | Implementation | Evidence | Tests | Status | Gap | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R14 | Advertiser registration | `create_organization` → PENDING, creator is OWNER | [apps/advertising/services.py:191-210](../../apps/advertising/services.py#L191-L210) | `test_organizations.py` | Verified | — | — |
| R15 | Organization approval | `review_organization` (staff perm), DB state machine | [services.py:260-290](../../apps/advertising/services.py#L260-L290), [migrations/0002_integrity.py](../../apps/advertising/migrations/0002_integrity.py) | `test_organizations.py` | Verified | No notification to applicant (CC-009) | P2 |
| R16 | Owner / Manager permissions | `_require_member(owner=…)`; last-owner protection | [services.py:160-170, 377-397](../../apps/advertising/services.py#L160-L170) | `test_organizations.py`, `test_web.py` | Verified | — | — |
| R17 | Team invitations | Email-matched in-app invitations | [services.py:292-375](../../apps/advertising/services.py#L292-L375) | `test_organizations.py` | **Partial** | Invitee is **not emailed**; must sign in to discover it | P2 (CC-009) |
| R18 | Organization isolation | `organization_for` / `campaign_for` scope every lookup; NotFound for others | [services.py:131-158](../../apps/advertising/services.py#L131-L158) | `test_web.py`, `test_concurrency.py` | Verified | — | — |
| R19 | Video upload & management | Staff-only upload (Bunny TUS / demo), categories, publication | [apps/administration/views.py:195-330](../../apps/administration/views.py#L195-L330), [apps/videos/services.py](../../apps/videos/services.py) | `apps/videos/tests` (Bunny mocked) | Implemented; Bunny unverified live | Advertisers cannot upload their own creatives (staff grant) | P2 (CC-018) |
| R20 | Bunny processing synchronisation | Poll-based `videos.sync_status` / `sweep_unfinished` jobs | [apps/videos/jobs.py](../../apps/videos/jobs.py) | `test_providers.py`, `test_lifecycle.py` (mocked) | Implemented, unverified (no live Bunny) | No webhook; credentials unset (B5) | P1 for demo (CC-002) |
| R21 | Protected video playback | Membership check + 10-min signed embed URL; keys server-side; no redirects | [videos/services.py:473-488](../../apps/videos/services.py#L473-L488), [providers/bunny.py](../../apps/videos/providers/bunny.py) | `test_providers.py`, `test_web.py` | Implemented, verified with mocks | Segment protection depends on Bunny CDN token auth setting (CC-025) | P3 |
| R22 | Campaign creation | Draft campaigns over granted videos; revisioned | [services.py:451-548](../../apps/advertising/services.py#L451-L548) | `test_campaigns.py` | Verified | — | — |
| R23 | Campaign targeting | Membership-plan targeting only | [services.py:859-861](../../apps/advertising/services.py#L859-L861) | `test_campaigns.py` | **Partial** | No geography/demographic/time-of-day targeting (requirement to be clarified) | P2 (CC-019) |
| R24 | Staff review & approval | `approve_campaign`/`reject_campaign` with expected revision; DB guard on reviewed fields | [services.py:617-666](../../apps/advertising/services.py#L617-L666) | `test_campaigns.py`, `test_concurrency.py` | Verified | No notification to advertiser | P2 (CC-009) |
| R25 | Publication / scheduling | Immediate activation on approval if due; otherwise `campaign_schedule` job | [services.py:644, 751-786](../../apps/advertising/services.py#L751-L786) | `test_campaigns.py` | Verified | Future starts need worker (CC-003) | — |
| R26 | Campaign suspension / cancellation | Staff pause (sticky), cancel, archive | [services.py:668-749](../../apps/advertising/services.py#L668-L749) | `test_campaigns.py` | Verified | "contact support" message but no support channel (CC-008) | — |
| R27 | Advertiser performance reporting | Counts by status + demo budget totals | [services.py:896-911](../../apps/advertising/services.py#L896-L911), `templates/components/campaign_report.html` | `test_web.py` | **Partial** | Verified views / unique viewers / spend shown as "not available yet" although `verified_views()` exists | P2 (CC-010) |

## D. Viewing, anti-fraud and rewards

| # | Requirement | Implementation | Evidence | Tests | Status | Gap | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R28 | Sponsored-video access | `available_campaigns`, `start_session` (member lock, eligibility, own-campaign block, limits) | [apps/viewing/services.py:157-222](../../apps/viewing/services.py#L157-L222) | `test_sessions.py`, `test_web.py` | Verified | Requires ACTIVE membership (CC-001) and playable video (CC-002) | P1 |
| R29 | Viewing verification | Server-credited watch time ≤ server-observed time; coverage; thresholds | [services.py:346-406, 454-501](../../apps/viewing/services.py#L346-L406) | `test_sessions.py`, `test_concurrency.py` | Verified (server rules) | Browser→server path untested (CC-015) | P2 |
| R30 | Anti-fraud enforcement | Rate limits, sequence/replay checks, seek/speed/gap flags, review queue, recent-rejection escalation | [services.py:409-433](../../apps/viewing/services.py#L409-L433) | `test_sessions.py` | Verified (rules) | No device/IP/multi-account signals; client heartbeats are forgeable at real-time pace | P2 (CC-014) |
| R31 | Reward qualification | `reward_eligibility` + `evaluate_session` (policy at finalisation, budget lock) | [apps/rewards/services.py:234-325](../../apps/rewards/services.py#L234-L325) | `test_rewards.py`, `test_concurrency.py` | Verified (demo) | — | — |
| R32 | Reward reversal | Fraud revocation signal, staff reversal, `reconcile_reversals` job | [services.py:352-412](../../apps/rewards/services.py#L352-L412), [viewing/services.py:578](../../apps/viewing/services.py#L578) | `test_rewards.py` | Verified (demo) | — | — |
| R33 | Rewards funded only by advertiser funds | Real ledger rules + locked features; demo rewards consume `demo_budget_amount` | [apps/wallets/migrations/0002_ledger_integrity.py](../../apps/wallets/migrations/0002_ledger_integrity.py), [rewards/services.py:276-322](../../apps/rewards/services.py#L276-L322) | `apps/wallets/tests/test_database.py` | Real: enforced (locked); Demo: **not linked to deposits** | Demo cannot show deposit→allocation→reward flow | P2 (CC-011) |

## E. Financial demonstration modules

| # | Requirement | Implementation | Evidence | Tests | Status | Gap | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R34 | Sandbox payments | Intents, signed callbacks, authoritative fetch, reconciliation, settlement, sandbox journal | [apps/payments/services.py](../../apps/payments/services.py) | `apps/payments/tests` | Verified (sandbox) | Disabled on Render demo unless explicitly allowed | — |
| R35 | Reconciliation | Field-by-field reconcile → exceptions queue; staff resolution | [services.py:421-460, 732-772](../../apps/payments/services.py#L421-L460) | `test_payments.py` | Verified | — | — |
| R36 | Refunds / chargebacks | Two-person refund approval; disputes from provider chargeback | [services.py:588-730](../../apps/payments/services.py#L588-L730) | `test_payments.py` | Verified (sandbox) | — | — |
| R37 | Wallet management | Wallet per member (3 ledger accounts), restrictions with lift workflow, reconciliation | [apps/wallets/wallet_services.py](../../apps/wallets/wallet_services.py) | `test_member_wallets.py`, `test_wallet_web.py` | Verified | Balances locked at TZS 0 by design | — |
| R38 | Demo withdrawals | Staff-run demo state machine, SoD, sandbox payouts, uncertain-state reconciliation | [apps/withdrawals/services.py](../../apps/withdrawals/services.py) | `test_withdrawals.py`, `test_web.py` | Verified (demo) | Payout simulation needs sandbox active; thin concurrency coverage (CC-021) | P3 |
| R39 | Real money (payments, rewards, withdrawals) | Locked in code and DB | [apps/core/features.py:49-52](../../apps/core/features.py#L49-L52) | `test_features.py`, `test_database.py` | **Intentionally disabled** | Requires a separately approved real-money architecture | Out of scope |
| R40 | Future payment provider (AzamPay or other) | Provider-neutral interface only | [apps/payments/providers/base.py](../../apps/payments/providers/base.py) | — | **Missing (deferred)** | No real provider adapter | Out of scope |

## F. Operations, administration and cross-cutting

| # | Requirement | Implementation | Evidence | Tests | Status | Gap | Pri |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R41 | Administrative operations console | `/manage/` permission-aware sections, queues, decisions | [apps/administration/views.py](../../apps/administration/views.py) | `test_dashboard.py`, `test_operations_admin.py` | Verified | Needs multiple privileged staff for SoD flows (CC-012) | P2 |
| R42 | Notifications | Email outbox for 4 account emails | [apps/notifications/services.py](../../apps/notifications/services.py) | `test_outbox.py` | **Partial** | No workflow emails, no in-app notifications, no preferences | P2 (CC-009) |
| R43 | Support | — | `apps/support/*` boilerplate | none | **Missing** | No tickets, FAQs, contact form or staff queue | P2 (CC-008) |
| R44 | Reports & analytics | Admin overview summaries; advertiser status counts; `audit_export` CLI | [apps/administration/summary.py](../../apps/administration/summary.py), `apps/reports/*` boilerplate | `test_dashboard.py` | **Missing (app) / Partial (summaries)** | No reports module, exports, date-range analytics, campaign performance | P2 (CC-007) |
| R45 | Audit logging | Append-only events, business + security catalogues, request context | [apps/audit](../../apps/audit) | `apps/audit/tests` | Verified | DB guarantee needs role separation (CC-005) | P1 (prod) |
| R46 | Background processing | Durable jobs, leases, retries, schedules, heartbeat | [apps/core/jobs](../../apps/core/jobs) | `test_jobs.py`, `test_jobs_concurrency.py` | Verified | Hosted worker is paid-only (CC-003) | P1 (demo) |
| R47 | Deployment operations | Render scripts, `check --deploy`, health probe, guide | [scripts/render](../../scripts/render), [docs/deployment/render.md](../deployment/render.md) | `test_deployment.py` | Implemented; **no deployment completed or verified** | Blockers B1–B7 open | P1 (CC-003–006) |
| R48 | Internationalisation (English/Swahili) | `LANGUAGES` en/sw, `locale/` | [config/settings.py:402-409](../../config/settings.py#L402-L409) | — | Implemented, completeness unverified | Swahili catalogue coverage not measured | P3 |

## G. Original roadmap items that remain open

| Roadmap item | Current state | Mapping to proposed plan |
| --- | --- | --- |
| Notifications (beyond account email) | Partial | Phase 14 batch 14-F |
| Support | Not started | Phase 14 batch 14-F |
| Reports & Analytics | Not started (summaries only) | Phase 14 batch 14-G |
| System-wide testing (browser/E2E) | Not started | Phase 14 batch 14-H / Phase 15 |
| Deployment hardening ("Phase 16" per [README.md:187](../../README.md#L187)) | Documented, not executed | Phase 16 (unchanged) |
| Formal acceptance | This audit prepares it | Phase 17 (see [10_corrective_roadmap.md](10_corrective_roadmap.md)) |
| Real-money activation (payments, rewards, withdrawals, AzamPay) | Intentionally locked | Separate reviewed programme; **not** renumbered into Phase 14 |
