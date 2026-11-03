# Phase 14-C — Member Video Journey, Verified-View Reporting and Demo Readiness

**Status:** implemented, uncommitted, awaiting owner review · **Baseline:** `main` @ `fa08ee0`
**Audit findings addressed:** CC-010 (advertiser reporting not connected to verified viewing); CC-002 (demo video
source) — readiness tooling and runbook; end-to-end journey verified through the real services and pages.
No migrations. No financial code, lock or invariant changed.

## 1. What already existed (reused, unchanged)

Member video library and detail pages, membership-gated signed playback (`videos.services.authorize_playback`,
Bunny embed tokens or the demo `<video>` sample), sponsored campaign list, server-owned watch sessions with JSON
heartbeats and anti-fraud rules, staff review/revocation, four-eyes demo reward policies and evaluation, and the
Phase 14-A complimentary grant. Phase 14-C connects them and makes their results visible.

## 2. Implemented

| Change | Where |
| --- | --- |
| `campaign_performance(campaigns)`: sessions, verified views, unique verified viewers, awaiting review, not verified — two aggregate queries; "verified" is the reward engine's definition, so revoked views drop out; counts only | `apps/viewing/services.py` |
| `organization_report(org, for_advertiser=…)` adds viewing figures; new `campaign_report(campaign, …)` adds per-campaign viewing figures and **simulated** demo-reward figures (recorded, reversed, demo budget used / allocated) | `apps/advertising/services.py` |
| Organization summary tiles: verified views, unique viewers, sessions; **"Spend: not applicable (no real money is charged)"** replaces the three "not available yet" placeholders | `templates/components/campaign_report.html` |
| New performance panel on the advertiser campaign page and the staff campaign review page, with a "Demo rewards (simulated, not money)" section | `templates/components/campaign_performance.html`, `templates/advertising/campaign_detail.html`, `templates/administration/campaigns/detail.html`, views |
| D12 support (decision pending): `DJANGO_ADVERTISING_REPORT_MIN_COUNT` shows non-zero counts below N as "fewer than N" to advertisers; default 0 = exact; staff always exact | `config/settings.py`, `report_figure()` |
| `manage.py demo_readiness`: read-only PASS/WARN/FAIL check of every demo precondition (restricted mode, features, video source and sample clip, verifiable videos, plans and entitlement coverage, members with access, grant availability, eligible campaigns, approved reward policies, worker, email, two-person approvers, 13 lock triggers + 2 constraints, real-money locks, sandbox state); exit 1 on any FAIL | `apps/administration/management/commands/demo_readiness.py` |
| Docs | `docs/architecture/advertising.md` (Reporting) |

The five financial categories stay separate: the reports show no real ledger amount (none exists), no sandbox
payment figure, simulated rewards and demo budgets only under explicit "simulated / demo / never charged" labels,
and nothing is summed across categories.

## 3. Tests

| Test module | Covers |
| --- | --- |
| `apps/advertising/tests/test_reporting.py` (8) | Counts from server outcomes (verified vs started vs unfinished); simulated reward figures; revoked views excluded; organization isolation (a rival's sessions never counted); two queries for any number of campaigns; D12 suppression for advertisers only; advertiser pages show results, "Spend: not applicable" and the simulated labels; staff page exact; outsider gets 404 |
| `apps/administration/tests/test_demo_journey.py` (7) | **End-to-end through the pages:** complimentary grant → sponsored list → start session (HTTP) → demo `<video>` with the sample clip → JSON heartbeats (HTTP) → verified → four-eyes policy → evaluation → simulated reward on the member's page → advertiser report counts the view → wallet 0 and no ledger, payment, withdrawal or funding change (only `DemoReward` and its demo budget counter change); access ends at grant expiry (session refused). `demo_readiness`: prepared environment has no FAIL; missing sample clip, no way to grant access, ineligible campaign each FAIL; command issues no INSERT/UPDATE/DELETE and exits 1 on failure |
| `apps/advertising/tests/test_web.py` | One assertion updated: it checked the old "not available yet" placeholder; it now checks real figures (0) and "Spend: not applicable" |

## 4. Verification

Executed 2026-11-04 on the isolated `test_clipcash_db` (development database only read, by `demo_readiness`).

| Command | Result |
| --- | --- |
| `python -W default manage.py test apps --keepdb -v 2` (full suite) | **967 run, 967 passed, 0 failures, 0 errors, 0 skipped; 518.7 s** (525 s wall; the machine was slower than in earlier runs — 952 tests took 253.8 s in Phase 14-B) |
| Financial-integrity classes in that run | `DirectSqlBypassTests` 14, `ZeroMoneyTests` 3, `RealWithdrawalTests` 2, `FeatureFlagTests` 9, `FinancialInvariantTests` 2 — all passed |
| Focused: `test_reporting`, `test_demo_journey`, `test_web` (advertising) | 29 run, OK |
| Focused: `apps.advertising`, `apps.viewing`, `apps.rewards`, `apps.videos` + journey | 195 run, OK after updating the one placeholder assertion |
| `manage.py check` / `makemigrations --check --dry-run` / `showmigrations` | No issues / No changes detected / 58 applied, 0 pending |
| `git diff --check` | Clean |
| `manage.py demo_readiness` on the local development database (read-only) | NOT READY — 6 failing (advertisers feature off, no sample clip, no video with a known length, no active plan, grants off, no eligible campaign), 6 warnings; financial locks PASS |

Test count 952 → 967 (+15). Warnings: only the pre-existing `RemovedInDjango60Warning`, a Bunny-mock `ResourceWarning`
and the runner-guard `UserWarning`. Changes in `apps/advertising` and `apps/viewing` are read-only reporting
functions and views: no model, migration, trigger, lock or write path changed (checked in the diff); nothing in
`apps/wallets`, `apps/payments`, `apps/withdrawals` or `apps/rewards` changed.

## 5. Demonstration runbook (local or isolated demo host)

1. Environment: `DJANGO_RESTRICTED_DEMO=True`, `DJANGO_MEMBERSHIPS_COMPLIMENTARY_GRANTS=True`; video source either
   Bunny (`VIDEO_PROVIDER=bunny` + the four `BUNNY_STREAM_*` values, CDN token authentication enabled in Bunny) or,
   on an isolated demo only, `VIDEO_DEMO_ALLOWED=True` and `VIDEO_DEMO_SAMPLE_URL` pointing to a clip **you made or
   are licensed to use** (e.g. placed under `static/videos/`).
2. Django admin: enable Memberships, Videos and Advertisers in Site configuration; create an ACTIVE plan whose
   entitlements include the videos' required entitlement.
3. `/manage/videos/`: create and publish the video; for demo videos set the duration to the clip's **real length**
   (verification needs `VIEWING_MIN_WATCH_PERCENT`, default 90 %, of a video of at least 5 s).
4. Advertiser: apply (verified account), staff approve the organization and grant the video; create and submit a
   campaign; staff approve it.
5. Reward policy: one Super Administrator drafts, a **second** approves (decision D2 for the second account).
6. Member: ACTIVE with a confirmed email; Super Administrator grants a complimentary membership with an expiry.
7. Run `python manage.py demo_readiness` until it reports **READY**; run `python manage.py run_jobs` (or leave the
   worker running) so rewards are evaluated automatically.
8. Walk through: member watches the sponsored video → result page → demo rewards → wallet (TZS 0); advertiser sees
   verified views; staff see the same figures and the review queue.

## 6. Not done / pending

| Item | Reason |
| --- | --- |
| Live Bunny playback, the `player.js` heartbeat bridge in a real browser, CDN token authentication (CC-025) | No Bunny credentials; must be tested by a person on a configured host |
| A licensed sample clip in the repository | Cannot be created or licensed by the assistant; the owner supplies it |
| Dedicated `viewing` feature flag (CC-026) | Would need a SiteConfiguration field (migration); low value; not done |
| CC-011 (demo rewards capped by sandbox allocations) | Owner decision D6 not taken |
| D12 threshold value | Owner decision; mechanism ready (default exact counts) |
| Browser checks of the new panels (keyboard, 360 px, screen reader) | Manual (Phase 16) |
