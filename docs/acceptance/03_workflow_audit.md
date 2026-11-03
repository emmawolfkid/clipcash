# 03 — End-to-End Business Workflow Audit (Phase 13C)

Method: each journey was traced from URL → view → service → model/trigger → job, using source inspection and
the existing PostgreSQL test suite (860 passing). **No journey was exercised in a real browser** and no external
provider was contacted; browser behaviour is therefore unverified (see CC-015).

Legend for each step: ✅ works as traced · ⚠️ works with a condition or gap · ⛔ blocked in the stated environment.

## 1. Member journey

| Step | Interface | Service authority | Transaction / locking | State change | Audit | Feedback | Tests | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Register | `accounts:register` | `accounts.services.register` | atomic; per-IP + per-email throttle | User PENDING; outbox email | `account.registration.started` | Uniform "check your email" page | `test_registration.py` | ⚠️ closed when `RESTRICTED_DEMO_MODE` (default outside DEBUG) |
| Verify (activate) | `accounts:activate` | `activate_account` | token moved to session; atomic | PENDING → ACTIVE + password | yes | complete page | `test_verification.py` | ⚠️ email needs worker + SMTP (CC-003) |
| Login | `accounts:login` | `ThrottledLoginMixin` | throttle counters | session | login success/failure/throttled | form errors | `test_login.py` | ⚠️ site-wide lockout risk behind proxy (CC-004) |
| Membership selection | `memberships:choose` | `choose_plan` | member row lock | PENDING subscription | `membership.subscription.created` | redirect to my membership | `test_lifecycle.py` | ✅ |
| Membership activation | none in product | `grant_complimentary` (CLI) / `activate_with_verified_payment` (locked) | member + subscription locks | PENDING → ACTIVE | `membership.subscription.activated` | — | `test_lifecycle.py` | ⛔ **no in-product path** (CC-001) |
| Eligibility | every playback/viewing call | `memberships.access.check_access` | read-only, DB each time | — | denial events | denial message | `test_database_and_access.py` | ✅ |
| Sponsored-video access | `viewing:list` → `viewing:campaign` | `available_campaigns`, `start_session` | member row lock; `select_for_update` sessions | WatchSession CREATED | `viewing.session.started` / `.denied` | messages | `test_sessions.py` | ⛔ until membership active and video playable (CC-001, CC-002) |
| Verified viewing | `viewing:session` + `events` JSON | `record_event`, `_close` | session row lock per event | CREATED → IN_PROGRESS → FINALIZED/INVALIDATED/EXPIRED | finalised / fraud flagged | progress bar + status text | `test_sessions.py`, `test_concurrency.py` | ⚠️ placeholder player cannot verify (CC-002); browser path untested (CC-015) |
| Simulated reward | job `rewards.evaluate_pending` or staff "evaluate" | `evaluate_session` | session → budget lock order | DemoReward QUALIFIED/REJECTED | `rewards.decision.*` | `rewards:history` | `test_rewards.py` | ⚠️ needs approved RewardPolicy (four-eyes, 2 staff) and worker or manual evaluation |
| Wallet visibility | `wallets:wallet` | `ensure_wallet`, `balances` | idempotent creation | Wallet + 3 accounts at 0 | wallet events | TZS 0 with notices | `test_member_wallets.py` | ✅ (by design shows 0; demo rewards are **not** credited) |
| Demo withdrawal eligibility | `withdrawals:overview` | `withdrawal_eligibility` | read-only | — | — | reasons list | `test_withdrawals.py` | ✅ always "not eligible" for real withdrawals (by design) |

**Broken transitions / misleading messages found**
* The journey **stops at membership activation** unless an operator runs
  `grant_complimentary_membership` with the grants flag enabled (CC-001).
* Paused-by-staff campaigns tell advertisers to "contact support"
  ([apps/advertising/services.py:700-701](../../apps/advertising/services.py#L700-L701)) but no support channel
  exists beyond an optional footer email (CC-008).
* Inline comment in [apps/memberships/access.py:48-49](../../apps/memberships/access.py#L48-L49) says a waived demo
  account "cannot obtain" a membership without a verified email; `grant_complimentary` does not check email
  verification, so it can (CC-024).

## 2. Advertiser journey

| Step | Interface | Service | Controls | Tests | Result |
| --- | --- | --- | --- | --- | --- |
| Application | `advertising:onboard` | `create_organization` | verified email, non-staff, one pending application per user, PENDING enforced by trigger | `test_organizations.py` | ✅ |
| Organization approval | `/manage/advertisers/<id>/` | `review_organization` | staff perm `advertising` review; expected-status stale check; DB state machine | `test_organizations.py` | ✅ (no notification to applicant — CC-009) |
| Owner/Manager authorisation | `advertising:team` | `invite_member`, `accept_invitation`, `remove_member` | owner-only invites/revokes; last owner kept; email match | `test_organizations.py`, `test_web.py` | ⚠️ invitation not emailed (CC-009) |
| Video selection | campaign form (granted videos) | `grant_video` (staff), `video_authorized` | only granted, non-archived videos | `test_campaigns.py` | ⚠️ advertisers cannot upload creatives (CC-018) |
| Campaign creation & targeting | `advertising:campaign_create` | `create_campaign`, `_clean_campaign` | owner/manager; plan targeting; demo budget labelled "not money" | `test_campaigns.py` | ⚠️ plan-only targeting (CC-019) |
| Staff review | `/manage/campaigns/<id>/` | `approve_campaign`/`reject_campaign` | review perm; `expected_revision`; reviewed fields frozen by trigger | `test_campaigns.py`, `test_concurrency.py` | ✅ |
| Publication | approval (if due) / `campaign_schedule` job | `_activate_if_due` | org approved, video available, dates | `test_campaigns.py` | ⚠️ future starts need worker (CC-003) |
| Performance reporting | `advertising:organization` | `organization_report` | own organisation only | `test_web.py` | ⚠️ verified views/viewers/spend shown "not available yet" (CC-010) |
| Sandbox funding (optional) | `payments:funding` | `create_deposit_intent`, `allocate`, `release_allocation` | owner-only; same-organisation trigger | `apps/payments/tests` | ⛔ on a `PAYMENTS_MODE=disabled` deployment (by design) |

## 3. Administrator journey

| Step | Evidence | Result |
| --- | --- | --- |
| Staff authentication | `/admin/` uses throttled `AdminLoginForm` ([apps/accounts/admin.py:14](../../apps/accounts/admin.py#L14)); `/manage/` uses `staff_member_required` ([apps/administration/views.py:59](../../apps/administration/views.py#L59)) | ✅ |
| Permission-aware dashboard | `console_links()` + `admin_summary.overview(user)` show only permitted sections ([views.py:70-86](../../apps/administration/views.py#L70-L86)) | ✅ |
| Operational queues | advertisers, campaigns, viewing review, rewards, payments exceptions, wallet reconciliation, withdrawal exceptions | ✅ |
| Review actions | Every POST re-checks the permission in the view **and** the owning service (e.g. [views.py:443-465](../../apps/administration/views.py#L443-L465) → `advertising.review_organization`) | ✅ defence in depth |
| Financial demo monitoring | payments list/detail/simulator (sandbox only), wallets, withdrawals | ⚠️ payments section hidden unless sandbox active |
| Audit records | Django admin `AuditEvent` (view-only, `can_view_audit`), security page | ✅ |
| Reports | Overview counts only | ⚠️ no reports module (CC-007) |
| Support management | — | ⛔ missing (CC-008) |
| Separation of duties | reward policy approver ≠ creator; refund approver ≠ requester; demo-withdrawal creator ≠ approver ≠ payout initiator; ≥ TZS 500,000 needs 2 approvers | ⚠️ requires several distinct privileged staff accounts, provisioned manually by a superuser (CC-012) |

## 4. Financial demonstration journey (sandbox)

`payments:intent` / advertiser deposit → staff simulator (`apps/payments/simulator.py`) signs a notice →
`SandboxCallbackView` (CSRF-exempt, HMAC) → `receive_callback` → `process_provider_update` fetches the
authoritative sandbox record **outside** any transaction, then in one transaction records a unique
`ProviderEvent` (replays become `DUPLICATE`) and `_apply` → `_reconcile` (environment, reference, currency,
amount, membership state) → `SandboxPosting` (idempotent key, append-only trigger) → `_settle` →
membership **preview** or advertiser `SandboxAdvertiserFunds`.

* ✅ Callback amounts are never trusted ([apps/payments/providers/base.py](../../apps/payments/providers/base.py)).
* ✅ Event de-duplication via unique `(provider, event_id)` + `IntegrityError` handling
  ([apps/payments/services.py:330-357](../../apps/payments/services.py#L330-L357)).
* ✅ Sandbox postings never touch `wallets_journalline` (module docstring + no ledger import beyond posting-type rules).
* ⚠️ Only available when `sandbox_active()` (DEBUG, or `DJANGO_PAYMENTS_SANDBOX_ALLOWED=True`); the documented Render
  demo sets payments `disabled`, so this journey is **not demonstrable there** without a configuration decision.

Simulated rewards, wallet restrictions and demo withdrawals are covered in
[05_financial_integrity.md](05_financial_integrity.md).

## 5. Failure and reversal journeys

| Scenario | Handling | Evidence | Tests | Assessment |
| --- | --- | --- | --- | --- |
| Failed verification link | Uniform `link_invalid` 400; tokens bound to state | [apps/accounts/views.py:96-151](../../apps/accounts/views.py#L96-L151) | `test_verification.py` | ✅ |
| Expired sessions | Backend rejects non-ACTIVE; `AccountStatusMiddleware` flushes; 12 h cookie age | [apps/accounts/middleware.py](../../apps/accounts/middleware.py) | `test_middleware.py` | ✅ |
| Duplicate requests | Form tokens / idempotency keys on intents, demo withdrawals, ledger; duplicate viewing starts return same session | services | concurrency tests | ✅ |
| Concurrent operations | `select_for_update` with fixed lock order; per-member locks | e.g. [wallets/services.py:223-242](../../apps/wallets/services.py#L223-L242) | 14 `TransactionTestCase` modules | ✅ (withdrawals: only 2 concurrency tests — CC-021) |
| Campaign suspension | Open sessions invalidated on next event/playback (`_revocation_reason`) | [viewing/services.py:238-248](../../apps/viewing/services.py#L238-L248) | `test_sessions.py` | ✅ |
| Fraudulent views | Flags → `SUSPICIOUS_REVIEW`; staff review/revoke → reward reversal signal | [viewing/services.py:409-501, 578](../../apps/viewing/services.py#L409-L501) | `test_sessions.py`, `test_rewards.py` | ✅ (heuristic limits — CC-014) |
| Reward revocation | `reverse_reward`, `reconcile_reversals` job | [rewards/services.py:352-412](../../apps/rewards/services.py#L352-L412) | `test_rewards.py` | ✅ |
| Payment callback replay | Duplicate event → `DUPLICATE` outcome + security event | [payments/services.py:330-357](../../apps/payments/services.py#L330-L357) | `test_payments.py` | ✅ |
| Refunds and chargebacks | Two-person refund; chargeback opens dispute on settled intents | [payments/services.py:588-730](../../apps/payments/services.py#L588-L730) | `test_payments.py` | ✅ (sandbox) |
| Failed background jobs | Retry with backoff; `on_failure` hooks (e.g. outbox FAILED); lease recovery; stale detection | [notifications/services.py:106-121](../../apps/notifications/services.py#L106-L121), [core/jobs](../../apps/core/jobs) | `test_jobs.py`, `test_jobs_concurrency.py` | ✅ (no external alerting — CC-016) |
| Demo withdrawal rejection/failure | DB state machine; timeout → `DEMO_UNCERTAIN`, reconciled with same reference, never re-sent | [withdrawals/migrations/0002_integrity.py](../../apps/withdrawals/migrations/0002_integrity.py), [services.py:584-706](../../apps/withdrawals/services.py#L584-L706) | `test_withdrawals.py` | ✅ |
| Partial workflow failures | Ledger posting all-or-nothing; audit writes in same transaction; provider calls outside locks | [wallets/services.py:317-367](../../apps/wallets/services.py#L317-L367) | `test_posting.py` | ✅ |

## 6. Summary

The service layer is consistently well-structured: single service authority per action, explicit
transactions and lock ordering, stale-decision checks, audit events in the same transaction, and database
triggers as a second line of defence. The end-to-end **member** journey is nonetheless **not completable in the
product** without operator intervention (CC-001) and a playable video source (CC-002); several hand-offs between
modules (verified views → advertiser reports, sandbox funds → demo reward budgets, workflow notifications) are
not connected (CC-009, CC-010, CC-011).
