# UI-08: Final frontend acceptance

Base commit: `e081a64`. All changes below are uncommitted, awaiting project-owner review.

**Outcome:** the complete PostgreSQL suite is green (1,317 tests). The frontend is accepted on Chromium
browsers (Edge and Chrome) with the demo video provider.

| Question | Answer |
|----------|--------|
| Chromium frontend acceptance | **Accepted** on the evidence in this document |
| Production release readiness | **Not established.** Live Bunny, Firefox, Safari, real devices and a screen reader have not been tested, the Swahili catalogue does not exist, and the production gates remain open |

See [Remaining release blockers](#remaining-release-blockers).

## Scope inspected

95 page states with seeded local data, using real role sessions: public site, authentication, member
dashboard, video library and viewing, memberships, rewards, wallets, payments, withdrawals, advertiser
workspace, administration, notifications, support, reports and analytics, and real 403 and 404 pages.

The server ran with `DEBUG=False` against a throwaway PostgreSQL database, so error pages were the real
ones. All data was local seed data. No production data or live provider was used.

## Defects found and fixed

| # | Defect | Effect | Fix |
|---|--------|--------|-----|
| 1 | Escape with focus inside the open mobile menu returned focus to Menu but left the menu open | Keyboard users could not close the menu from inside it, on all shells | `member-nav.js`: move focus before closing |
| 2 | The general input border rule outranked the invalid-field rule | Text, email, password, number and date inputs showed no red border on any form; selects and textareas did | `forms.css`: selector with enough specificity |
| 3 | A partly clipped table action did not scroll into view when it took keyboard focus | "Remove" and "Leave" clipped on the advertiser team table at 320px | `shared-ui.js`: keyboard-only `focusin` scroll |
| 4 | Nothing told sighted users that a table scrolled sideways | Columns and actions could be missed on phones | `shared-ui.js`, `tables.css`, `base.html`: translatable hint, shown only while the table scrolls |
| 5 | Removing a payout destination took one click | Accidental removal (recorded as open in UI-05) | `destinations.html`: same disclosure and "Confirm removal" as team removal; same POST fields |
| 6 | Swahili was offered with no catalogue | English text on a page declaring `lang="sw"` | `language_tags.py`, `language_form.html`: offer a language only if it has a compiled catalogue |
| 7 | No favicon | Browser default icon, 404 in the console | `favicon.svg`, linked from `base.html` |
| 8 | Seven auth pages had a heading before the page title | First heading was not the `h1` | `accounts/layout.html`, `auth.css`: the intro title is now a paragraph with the same look |
| 9 | Library pages jumped from `h1` to `h3` | Heading order skipped a level | `videos/library.html`: visually hidden `h2` |
| 10 | Support ticket table had no caption | Table had no accessible name | `support/ticket_list.html`: visually hidden caption |
| 11 | Session help disclosure was below the touch target size | Hard to tap | `session.css`: more padding |

Defect 1 corrects earlier reports. The UI-06 and UI-07 menu checks tested Escape only with focus outside
the menu, so they did not catch it.

**One existing test was changed.** `apps/core/tests/test_public_auth_ui.py` now asserts the language form
only under `sw`, because English has nothing to switch to until a catalogue exists. New regression tests
are in `apps/core/tests/test_frontend_acceptance.py`.

**Several regression tests only read source text.** Tests for defects 1, 2, 3, 4, 7, 8 and 9 assert on
markup, CSS or script text. The behaviour itself was verified only in the browser passes below, which are
not part of the repository.

## Known issues verified

| Known issue | Finding |
|-------------|---------|
| Mobile tables | Hint added; all actions and last columns reachable. See [Responsive results](#responsive-results) |
| Staff inbox layout | Not changed. See below |
| Favicon | Fixed. It redraws the owner's attached mark; no official asset exists in the repository, so please replace it with one |
| Duplicate navigation | None: one Menu button at 320 to 768px, none at 1024px and above |
| Financial form confirmation | Destination removal fixed. Other single-click actions listed under [Financial safeguards](#financial-safeguards) |
| Raw status codes | Only on three staff pages, kept deliberately. See below |
| Translation catalogues | Absent. See [Translation limitations](#translation-limitations) |
| Django admin inconsistency | Not redesigned. Decision requested |

**Staff inbox.** It uses the member shell through a protected notifications template that has no `can`
context. Moving it to the console layout would touch protected notification behaviour, so it was left
alone. The header "Console" link remains available. Decision requested.

**Raw codes.** `adm-security` and `adm-withdrawal` show audit event names (such as `note_added`), and
`adm-wallet` shows ledger account codes (such as `member_reward_pending`). These are identifiers staff use
to trace records, and audit semantics are out of scope, so they stay. No raw codes appear on member or
advertiser pages in the seeded states.

**Django admin.** It keeps its default appearance and has no ClipCash favicon: `/favicon.ico` still returns
404 on direct requests and admin pages. Not changed.

**Cards.** Member pages still use cards (the dashboard has six). They predate this phase and were not
redesigned.

## Browser coverage

| Browser | Status | Coverage |
|---------|--------|----------|
| Edge 156 (Chromium) | Tested, headless | 95 states at 7 widths = 665 combinations on final code, plus real-key interaction runs. The last full interaction run predates only the table focus handler, which was re-tested separately |
| Chrome 156 | Tested, headless | 95 states at 390 and 1024px = 190 combinations, on final code. Same engine as Edge, so little independent evidence |
| Firefox | **Untested** | Not installed |
| Safari, including iOS | **Untested** | Not available on this machine |
| Real phones and tablets | **Untested** | Widths emulated by viewport |

Chrome tab-stop totals are not comparable with Edge: in Chrome the driver's Tab loop wrapped around the
page on some states and hit its cap. Focus outlines were present on every stop in both.

## Responsive results

Final Edge pass, widths 320, 375, 390, 640, 768, 1024 and 1440:

| Check | Result |
|-------|--------|
| Redirected to the wrong page | 0 |
| Page-level horizontal overflow | 0 of 665 |
| Low-contrast text found by the automated check | 0 |
| CSP violations / inline styles | 0 / 0 |
| Console errors | Only the expected 403 and 404 resource errors, on the 3 error-page states |
| Small touch targets at 390px | 0 (was 1) |

**Mobile tables.** 21 page states scroll sideways at 390px, and were kept as tables on purpose. UI-07
recorded 11 such states without listing them. The matching set today is 10 administration states
(`adm-analytics`, `adm-funding`, `adm-health`, `adm-payment`, `adm-payments`, `adm-reconciliation`,
`adm-report-advertising`, `adm-report-rewards`, `adm-rewards`, `adm-withdrawals`) plus `adv-funding`. The
other 10 are member-facing: `destinations`, `mine-active`, `mine-complimentary`, `mine-pending`, `payments`,
`rewards`, `support-list`, `watch-history`, `withdrawals` and `withdrawals-preview`.

Tested at 320 and 390px: 49 page-and-width combinations, 69 scrolling tables. Each one shows the hint, is a
keyboard stop, and brings its last column into view. A first scripted check flagged 14 tables for
"unreachable" actions; most were the script measuring too soon. Re-testing 13 of them with real Tab keys and
a pause found the 3 clipped stops of defect 3, and 0 after the fix. The 14th, `withdrawals-preview` at 390px,
was not re-tested; it renders the same table as `withdrawals`, which passed.

Minor, left as is: a few tables overflow by only a few pixels (`adm-analytics` at 390px is 364 of 368), so
the hint appears for a very small scroll.

## Navigation results

Real key presses at 390px on member, advertiser, console, staff inbox and public pages:

- One Menu button; closed on load; opens by keyboard; the next Tab stops enter the menu.
- Escape closes it and returns focus to Menu on all five (fixed, defect 1).
- Skip link is the first stop and moves focus to the content.
- The long console sidebar scrolls, and its current item is in view at 1024px.
- Permission-aware links: role differences were seen in the seeded role sessions; server-side permission
  tests are in the suite.

## Accessibility checks

**This is evidence from the checks listed, not an accessibility certification.** No WCAG audit was done.

Performed in Edge:

- Contrast: automated computed-colour check on every state; it does not assess text over images or gradients.
- Keyboard: 2,700 Tab stops across 95 states at 1024px, no missing focus outline.
- Names and structure: Chromium accessibility tree on every state, no unnamed controls, landmarks present,
  `lang="en"`; exactly one `h1` and no skipped heading levels; every table captioned.
- Errors, by submitting invalid forms on 8 forms (login, password reset, profile, email change, password
  change, support, campaign, advertiser profile): summary with `role="alert"`, focus moved into it,
  `aria-invalid` and `aria-describedby` set.
- 200% zoom: approximated by the 640px viewport. Browser zoom itself was not exercised.
- Forced colours: emulated on all 95 states, no overflow.
- Reduced motion: emulated on all 95 states, no moving elements.
- Modal dialogs: none exist, so there are no focus traps to test.

**Not performed:** any real screen reader (NVDA is not installed; Narrator could not be driven), so
announcements were checked from markup and the accessibility tree, never heard. Voice control and switch
access were also not tested.

**Known limitation:** payout destination errors come back as a `role="status"` message after a redirect,
with no field errors, and typed values are lost.

## Video verification

| Item | Evidence |
|------|----------|
| Library, detail, restricted and sponsored states | Rendered in the browser passes with the local demo provider |
| Restricted member message | Regression test and browser check |
| Loading, errors, progress and feedback in the player | `scripts/verify_ui04_player.mjs`: PASS, 9 scenarios, DOM mocks only |
| Verified status | `session-verified` state rendered; server rules covered by the suite |
| **Live Bunny playback, upload and signed tokens** | **Not tested: outstanding release gate** |

No actual video was played in a browser during this phase.

## Financial safeguards

- Demo, sandbox and simulated labels are present on payments, rewards, wallets, withdrawals and advertiser
  funding in the seeded states. Sandbox notice styling was sampled by the driver.
- Wording search across templates and forms found no earnings or payout promises. Every "real money" and
  "guarantee" hit says that no real money moves or that nothing is guaranteed.
- Masking: destinations show the last four digits, and a test asserts the full number is absent.
- Wallet restricted and unopened states and empty withdrawal states rendered. Permission boundaries
  are covered by the suite.
- No financial service, ledger, reward, withdrawal or reconciliation code changed, and no real-money
  feature was activated.

**Single-click actions left unchanged, for owner decision:** "Release" (allocation), "Cancel payment" and
"Cancel this choice". They act on sandbox or demo records only. They should get confirmation before any
real money is enabled.

## Translation limitations

- Hooks are in place: 1,820 translation tags across 141 templates. The only templates without `i18n` have
  no hard-coded visible text.
- `locale/` holds only `.gitkeep`, so **no Swahili catalogue exists**. None was written, because translations
  need human review. `msgfmt` is also not installed here.
- With no catalogue the language switcher is hidden, and it returns automatically once a compiled catalogue exists.
- Edge case: a visitor with an existing `sw` cookie, or a site whose `default_language` is `sw`, still gets
  `lang="sw"` over English text. Tested with a cookie: the switcher appears and they can switch back.

## Test results

Final run, on the final code, on PostgreSQL database `test_clipcash_db` through the guarded
`config.test_runner.PostgreSQLTestRunner`, with nothing else using it:

```
python -B manage.py test --keepdb --noinput
Found 1317 test(s).  Ran 1317 tests in 380.237s.  OK
```

Started 06:24 EAT, finished 06:31 EAT, exit 0.

### Time-of-day test correction

The first complete run (1,316 tests, started 00:35 EAT) had one failure:
`apps.reports.tests.test_analytics.SupportAndOperationsTests.test_period_today_is_included`.

The test passed the UTC date, while reporting periods are built in `Africa/Dar_es_Salaam`. From 00:00 to
03:00 EAT the UTC date is still the previous day, so the period ended before the member joined. The reporting
code was correct. Only `apps/reports/tests/test_analytics.py` changed; production analytics and timezone
settings did not.

- `test_period_today_is_included` now uses the Tanzanian date and asserts the same result.
- New `test_reporting_day_follows_tanzania_not_utc` sets a controlled clock and join time for six instants:
  23:59:59, 00:00:00, 00:30, 02:59:59, 03:00:00 and 12:00 EAT. It checks the reporting date, periods ending
  on that date and the day before, single-day periods, and what a period ending on the UTC date counts.
- Negative check, outside the repository: forcing reporting onto UTC in memory makes that test fail on exactly
  the three instants inside the window.

**Limits of this evidence:** the window was exercised on a controlled clock, because the final suite ran at
06:24 EAT, outside it. No complete suite has been run on the corrected code during the window. The earlier
in-window run found only this one failure, but it predates the correction.

### Other commands

| Command | Result |
|---------|--------|
| `python -B manage.py test apps.reports.tests.test_analytics --keepdb --noinput` | 26 tests, OK |
| `python -B manage.py test apps.core.tests.test_frontend_acceptance apps.core.tests.test_public_auth_ui --keepdb --noinput` | 29 tests, OK (before the test correction, which does not touch them; both modules are in the final run) |
| `python -B manage.py check` | No issues, exit 0 |
| `python -B manage.py makemigrations --check --dry-run` | No changes, exit 0 |
| `python -B manage.py migrate --check` | Exit 0 |
| `python -B scripts/verify_frontend_css.py` | PASS: 42 nonempty modules, 551 unique selector/condition pairs |
| `git diff --check` | Clean; new files have no trailing whitespace |
| `node scripts/verify_ui04_player.mjs` | PASS, 9 scenarios (DOM mocks) |

## Remaining release blockers

**Frontend evidence gaps**

1. Live Bunny playback, upload and signed tokens untested.
2. Firefox and Safari, including iOS, untested.
3. Real phones and tablets untested.
4. No real screen reader test.
5. Swahili catalogue absent; needs human translation and review.

**Production gates from earlier phases, not re-checked here**

Render has never been run; CC-005 on Render; migration `0003` applied first; staff permissions;
`check --deploy`; SMTP; background worker. `main` is 5 commits ahead of origin and includes `0003`, so
Render Auto-Deploy should be off before pushing.

**Decisions requested from the owner**

- Home hero: it reads "Watch clips. Know what counts." rather than the approved "WATCH CLIPS. EARN REWARDS.".
  The current wording avoids an earnings promise while rewards are demo-only.
- Staff inbox: stay in the member shell, or move to the console layout?
- Official logo and favicon files.
- Palette mismatch in the design image.
- Email-change sign-in behaviour, still open from Phase 17-A.
- Django admin: leave as it is, or theme it?
- Confirmation on the three single-click sandbox actions.
