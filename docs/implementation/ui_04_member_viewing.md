# UI-04 — Member discovery and viewing

Scope: frontend presentation only. No UI-05, staging, commits, pushing or deployment.
Baseline: branch `main`, HEAD `97ada9e9a34de55ec1c291100af3a173d10b2740`.
Existing UI-02, UI-03 and unfinished support/backend changes remain in place.

## Implementation

- Dashboard: personalized navy welcome, feature-aware discovery shortcuts and notification access. Existing membership, verified/review counts, simulated rewards, ledger balances, withdrawal restrictions, sandbox notices and recent videos retain their original data and conditions. Membership text explains that playback and sponsored eligibility are separate.
- Activity: dashboard/history navigation and more readable shared timeline spacing, with long text wrapping on narrow screens. Existing records and chronological order are unchanged.
- Library/category: responsive shared cards, clearer typography, accessible current-category state, existing search and query-preserving pagination, clear-search recovery and explicit separation from sponsored verification. No new search backend or invented availability.
- Details: existing authorization messages, signed playback URLs and provider integration remain intact. Library playback does not imply sponsored verification. Native media failures expose translated recovery feedback.
- Sponsored discovery/entry: advertiser identification, existing remaining-view/open-session values, eligibility recheck explanation, existing POST/CSRF start flow and financial notice.
- Sessions: server-counted progress is labelled separately from the player timeline; completion copy never implies an award. Existing verified, review, rejected, insufficient-evidence and expired outcomes remain server-controlled. Open sessions without playback have an explicit unavailable heading. Recovery help remains visible even where cross-origin iframe failures cannot be detected.
- History: labelled caption, all four column headers retained, horizontal keyboard-scroll enhancement reused from UI-02, tabular counted-time values and empty-state recovery.

## Files in UI-04

Created:

- `static/css/pages/members/dashboard.css`
- `static/css/pages/videos/library.css`
- `static/css/pages/viewing/list.css`
- `static/css/pages/viewing/history.css`
- `static/js/media-feedback.js`
- `apps/core/tests/test_member_viewing_ui.py`
- `scripts/run_ui04_regressions.py`
- `scripts/verify_ui04_player.mjs`
- `docs/implementation/ui_04_member_viewing.md`

Modified (some already contain accepted, uncommitted UI-02/UI-03 work):

- `templates/members/dashboard.html`, `activity.html`
- `templates/videos/library.html`, `detail.html`
- `templates/viewing/list.html`, `campaign.html`, `session.html`, `history.html`
- `static/css/components/media.css`, `lists.css`
- `static/css/pages/videos/detail.css`
- `static/js/video-watch.js`
- `apps/core/tests/test_design_system.py` (library stylesheet expectation)

No empty activity/campaign stylesheet was created: these pages reuse shared components.
New CSS is loaded only through each consuming template's `page_styles` block, after tokens, shared components and member layout. No inline CSS/JS or dependencies were introduced.

## Security and preservation

Only user feedback changed in `video-watch.js`. Sequence, position/state JSON contract, same-origin credentials, CSRF header, event cadence, trusted Bunny origins, source-window checks and Player.js subscriptions are unchanged. Progress remains server-supplied. Provider credentials, playback URL creation, services, authorization, evidence rules, rewards, wallet, payments, withdrawals, audit, jobs and schemas were not edited.

The nine protected notification files match their recorded SHA-256 hashes and have no Git diff. Unfinished support files were not edited. Index remains empty; HEAD unchanged. The dedicated test database is retained; no existing database was removed and no application/production migration was run.

## Verification

Commands (using the existing local venv interpreter; it requires execution outside the sandbox):

```powershell
.\venv\Scripts\python.exe scripts/run_ui04_regressions.py --database test_clipcash_ui04_20261104
.\venv\Scripts\python.exe manage.py test apps.core.tests.test_member_viewing_ui apps.core.tests.test_design_system --testrunner django.test.runner.DiscoverRunner --noinput
.\venv\Scripts\python.exe scripts/verify_frontend_css.py
.\venv\Scripts\python.exe manage.py check
git diff --check
```

The UI-04 runner extends the accepted UI-03 suite and includes **all** video and viewing tests, including concurrency/provider/lifecycle coverage. It uses the guarded project runner, an explicit initially idle PostgreSQL test database, an advisory lock and `keepdb=True`; no concurrent test runner was launched.

JavaScript: `scripts/verify_ui04_player.mjs` was imported with the available Node tool. **9 DOM-mock scenarios passed**, covering origin/source rejection, exact event payload/CSRF, server progress, rate limits, rejection, offline updates, enough-time wording, closed-session stop/reload and media error/recovery. This is not visual browser testing.

CSS: **30 nonempty modules, 396 unique selector/condition pairs**; no repeated properties, missing references or unreferenced modules. System check: no issues. `git diff --check`: passed (pre-existing CRLF normalization warnings only).

Initial database-free template checks found two incomplete test-fixture fields (user email and withdrawal count). The new fixtures were corrected; no backend or production behavior was changed to accommodate them.

The first full run executed **539 tests in 280.287s**, with **1 failure and 0 errors**: the new shortcut test accidentally checked the whole page and matched an existing sidebar link. The assertion now checks only the dashboard welcome panel, preserving existing navigation. The corrected focused template suite passed **14 tests in 1.768s**. A full isolated rerun follows below.

Source palette contrast calculations: navy/white **16.55:1**, navy/yellow **11.18:1**, slate/white **4.89:1**, slate/page background **4.56:1**. These calculations do not substitute for rendered-page accessibility testing.

PostgreSQL result: the corrected full rerun passed **539 tests in 284.957s**, with **0 failures and 0 errors**. The isolated database was preserved.

## Browser and remaining review

Browser inventory returned `browsers: []` and `The browser access host is unavailable`. No rendered-page or visual pass is claimed. Responsive CSS uses existing flexible grids, wrapping controls, bounded players, safe text wrapping and scrolling tables, with shared keyboard/focus/mobile navigation behavior retained.

Manual review remains at **320, 375, 390, 768, 1024 and 1440px**: dashboard greeting/shortcuts, long category names, search form, video grid, membership-denied player, sponsored entry, native/Bunny playback controls, session outcomes, history scroll/focus, mobile menu and keyboard traversal. Real Bunny playback/network failure behavior requires browser verification; cross-origin iframe load events cannot prove playback health.

All new strings use Django translation hooks, including JavaScript messages supplied through escaped data attributes. English/Swahili template rendering is covered. Human Swahili catalogue translation/review remains outstanding; existing backend-generated activity text is unchanged.

Acceptance recommendation: **accept UI-04 implementation and automated verification**, subject to the manual visual, keyboard and live Bunny playback review above.
