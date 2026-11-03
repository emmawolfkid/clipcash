# 14 — Manual Acceptance Checklist

Use on a **non-production** environment. Record evidence (screenshot file name, audit-event id, or log line) in the
last column. Mark each row Pass / Fail / Blocked and reference an issue ID when not passing.

**Global prerequisites:** environment URL and mode recorded (DEBUG, `RESTRICTED_DEMO_MODE`, `PAYMENTS_MODE`,
`VIDEO_PROVIDER`); worker heartbeat fresh in `/manage/health/`; demo data per
[11_client_demo_readiness.md §3](11_client_demo_readiness.md); browser + mobile device (≤ 400 px) available.

Tester: ____________  Date: ____________  Build/commit: ____________

## A. Member

| # | Step | Expected result | Result | Evidence |
| --- | --- | --- | --- | --- |
| A1 | Open `/` signed out | Homepage loads with styles; restricted-demo wording if enabled; no earnings promise | | |
| A2 | Open `/accounts/register/` | Demo: 403 "registration closed"; otherwise email-only form | | |
| A3 | (Open registration) Register a new address | "Check your email" page; activation email delivered within worker interval | | |
| A4 | Register the same address again | Identical response; no indication the account exists | | |
| A5 | Follow activation link, set password | Account ACTIVE; link cannot be reused | | |
| A6 | Sign in with wrong password 5× | Sixth attempt shows throttled message; other users unaffected (CC-004) | | |
| A7 | Sign in correctly | Dashboard shows modules allowed by feature flags | | |
| A8 | Password reset for existing and non-existing address | Same confirmation page; email only for existing ACTIVE account; 1 h link | | |
| A9 | Browse `/memberships/`, open a plan | No investment/earnings promise; price shown | | |
| A10 | Choose plan | PENDING subscription in "My membership" with clear next step | | |
| A11 | Staff grants complimentary membership (CLI now; console after 14-A) | Subscription ACTIVE, source complimentary, amount 0; audit event | | |
| A12 | `/videos/` and a video page | Playback appears (Bunny iframe or demo `<video>`); without membership → access explanation | | |
| A13 | `/watch/` → start a sponsored campaign | Session page; status announces progress (screen reader) | | |
| A14 | Watch to the end without seeking | Outcome "verified evidence" | | |
| A15 | Repeat with large forward seek | Not verified or sent to review; no reward | | |
| A16 | Open two tabs on the same campaign | Same session reused; no double credit | | |
| A17 | After evaluation, open `/rewards/` | Demo reward listed with demo labelling; no money language | | |
| A18 | `/wallet/` | Wallet shows TZS 0 and the "wallets hold no money" notice | | |
| A19 | `/withdrawals/` | Real withdrawal shown as unavailable with reasons; demo notice | | |
| A20 | Add a payout destination | Saved with cooling period; details immutable afterwards | | |
| A21 | Try another member's URLs (subscription, session, demo withdrawal ids) | 404 for every one | | |
| A22 | Switch language to Swahili | Expect English today (CC-030) — record | | |
| A23 | Keyboard only: tab through header, menu, forms, player | Visible focus; logical order; skip link works | | |
| A24 | Mobile width | No horizontal scroll; member menu toggles | | |

## B. Advertiser

| # | Step | Expected result | Result | Evidence |
| --- | --- | --- | --- | --- |
| B1 | Verified member opens `/advertisers/new/` | Application form; submit creates PENDING organisation | | |
| B2 | Submit a second application while pending | Refused with clear message | | |
| B3 | Staff approves organisation | Status APPROVED; audit event; (no email today — CC-009) | | |
| B4 | Owner invites manager by email | Invitation pending; invitee sees it after sign-in (no email — CC-009) | | |
| B5 | Manager tries to invite / remove owner | Refused | | |
| B6 | Owner removes self as last owner | Refused ("must keep at least one owner") | | |
| B7 | Create campaign with granted video, plan targeting, demo budget | Draft saved; help text says demo figure is not money | | |
| B8 | Submit; staff rejects with reason; edit; resubmit; staff approves | Revision increments; approved campaign ACTIVE (or SCHEDULED if future) | | |
| B9 | Edit approved campaign | Creates a new revision requiring review | | |
| B10 | Organisation report | Status counts and demo budget; verified views placeholder (CC-010) | | |
| B11 | Access another organisation's URLs | 404 | | |
| B12 | Advertiser team member tries to watch own campaign | Refused ("own campaign") | | |
| B13 | (Sandbox env) Deposit → staff simulate pay + settle → allocate to campaign | Funds shown as sandbox; campaign funding status stays UNFUNDED | | |

## C. Administrator

| # | Step | Expected result | Result | Evidence |
| --- | --- | --- | --- | --- |
| C1 | Sign in at `/admin/` with wrong password repeatedly | Throttled | | |
| C2 | Staff without permissions opens `/manage/` sections | Only permitted sections visible; direct URLs → 403 | | |
| C3 | Content manager uploads a video (Bunny) | Upload completes; status syncs to READY via worker | | |
| C4 | Grant video to advertiser; revoke | Reflected in campaign form | | |
| C5 | Viewing review: approve/reject a suspicious session; revoke a verified one | Decisions recorded; linked demo reward reversed | | |
| C6 | Reward policy: staff A drafts, staff A tries to approve | Refused (four-eyes) | | |
| C7 | Staff B approves policy | Approved; previous policy retired | | |
| C8 | Run "evaluate" in demo rewards | Counts shown; rewards appear for members | | |
| C9 | Wallet: place restriction, request lift, decide lift (different staff) | State machine enforced | | |
| C10 | Demo withdrawal: create (staff A), approve (staff B), initiate payout (staff C), simulate paid/timeout | DEMO_* states only; timeout → uncertain → reconciled | | |
| C11 | Same person tries two roles in C10 | Refused | | |
| C12 | Operational health page | Worker heartbeat, backlog, cache, "payments disabled" statement | | |
| C13 | Audit log in Django admin | Events for every action above; cannot edit/delete | | |
| C14 | Non-superuser staff tries to change a role / group | Refused | | |

## D. Financial safety spot checks (read-only)

| # | Check | Expected | Result | Evidence |
| --- | --- | --- | --- | --- |
| D1 | `/manage/health/` | States real payments/payouts disabled | | |
| D2 | Any member wallet | TZS 0 in all three balances | | |
| D3 | Any campaign in Django admin | `funding_status` = UNFUNDED | | |
| D4 | Test suite on the build | `manage.py test apps --keepdb` → ≥ 860 tests, 0 failures, incl. `DirectSqlBypassTests`, `ZeroMoneyTests`, `RealWithdrawalTests` | | |

## E. Sign-off

| Role | Name | Decision | Date |
| --- | --- | --- | --- |
| Project owner | | | |
| Technical lead | | | |
| Client representative | | | |
