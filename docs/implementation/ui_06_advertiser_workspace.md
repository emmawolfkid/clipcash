# UI-06 — Advertiser workspace

Baseline: `04140e1`. Status: implemented and tested, **not committed**. Not deployed. Final visual and
accessibility acceptance stays with UI-08.

Scope: presentation of the existing advertiser workflows. No migration, authorization, ownership,
approval, eligibility, verified-view, financial or Bunny code changed. Real money stays locked.

## Pages inspected

Routes in `apps/advertising/urls.py` and templates in `templates/advertising/`: home, application,
overview, profile, team, campaign list, campaign create and edit, campaign detail, and reports, plus
`payments/funding.html`, which shares the advertiser layout.

## Genuine issues found

| Issue | Evidence | Fix |
|---|---|---|
| Card-heavy pages | 12 cards on the overview and on campaign detail, 7 on funding, 3 on reports | Hairline metric rows, section headings, tables, definition lists. 0 cards, except 2 clearly separate forms on funding |
| No page had its own title | The `h1` was always the organization name | Organization line, then tabs, then a true page title and primary action |
| Two Menu buttons at 768px and below | Measured on every page, including member and admin pages | One button that opens both menus; repeated links hidden |
| "New campaign" offered where the service refuses it | Suspended and rejected advertisers | Action removed; the create page explains why |
| "Create a draft" told to suspended advertisers | Seen in a screenshot | Separate empty state |
| "Changes requested" shown in danger red | `badge--rejected` | Attention colour |
| Zeros for campaigns that never ran | Campaign detail and reports | "No results yet" and "Has not run" |
| Tables needed sideways scrolling on phones | 6 page states at 390px | Secondary columns collapse; detail moves under the name. Only funding still scrolls |
| Uncaptioned tables | 7 page states | Captions on every table |
| Disabled button failed contrast | 4.33:1 | White background (shared rule) |
| Date inputs had no visible focus | 6 measurements, 2 inputs each | `:focus-within` outline (shared rule) |
| Team removal was one click | No confirmation step | Native disclosure with a named confirmation; no JavaScript |
| Unformatted amounts | `TZS 100000` | `tzs` filter |
| Stale help text | "Used when verified viewing is introduced" | Describes the limit `apps/viewing` already enforces |

## Design choices

- **Hierarchy on every page:** organization line, tabs, title with primary action, filters, content,
  then secondary notes.
- **Metrics are one quiet row:** at most five figures, separated by hairlines, in modest type.
- **Status counts link to the existing filter** (`?status=DRAFT`), so they are useful as well as
  informative, and zero counts are hidden.
- **Settings pages use a two-column pattern:** heading and explanation on the left, fields on the right,
  stacking on narrow screens. Verified, read-only fields are shaded with a dashed border.
- **Permanent actions are separated:** routine actions sit under the title; cancel and archive sit at
  the end of the page and keep their confirmation.
- **Phones get fewer columns, not stacked cards.** Real table semantics are kept.
- **Shared admin components stayed untouched:** `campaign_report.html` and `campaign_performance.html`
  are still used by the console, which is UI-07's scope.

## Files

| Kind | Files |
|---|---|
| Templates changed | `advertising/` `_campaign_table`, `_header`, `campaign_detail`, `campaign_form`, `campaign_list`, `home`, `onboard`, `organization`, `profile`, `reports`, `team`; `payments/funding` |
| Templates created | `advertising/` `_campaign_status`, `_form_errors`, `_new_campaign_action`, `_organization_fields` |
| CSS rewritten | `layouts/advertiser.css` |
| CSS created | `pages/advertising/campaign.css`, `pages/advertising/settings.css` |
| Shared corrections | `components/buttons.css` (disabled contrast), `components/forms.css` (date focus), `layouts/member.css` and `layouts/public.css` (menu), `static/js/member-nav.js` (one menu button; current tab kept in view) |
| Python | `apps/advertising/views.py`, `apps/advertising/forms.py` (below) |
| Tests and tools | `apps/advertising/tests/test_workspace_ui.py` (36 tests), `scripts/run_ui06_regressions.py`; one assertion in `apps/advertising/tests/test_reporting.py` |

## Read-only Python adjustments

- `views.py`: `OrganizationView` and `CampaignListView` add `prefetch_related("target_plans")` so the
  tables can show the audience without one query per row. The existing no-N+1 test still passes.
- `forms.py`: the help text of `max_views_per_member` was corrected. No validation changed.
- `test_reporting.py`: `"TZS 100 / 100000"` became `"TZS 100 of TZS 100,000"`, because the page now
  formats this figure. It is the only existing test assertion changed.

## Shared changes outside the advertiser area

The single Menu button and the shared CSS corrections also apply to member, notification and
administration pages. The menu was checked on all 40 sampled pages at 390px, including one member page
and one administration page. Those other roles' pages were not otherwise reviewed for appearance.

## Test results

Guarded runner, `test_clipcash_db`, `keepdb=True`, initially idle. No database was created or removed.

| Run | Command | Result |
|---|---|---|
| UI-06 and frontend regressions | `python -B scripts/run_ui06_regressions.py --database test_clipcash_db` | **1,040 tests in 429.184s, OK** |
| Remaining apps | `python -B manage.py test apps.audit apps.notifications apps.support apps.core --keepdb --noinput` | **358 tests in 97.405s, OK** |

The two runs overlap, so do not add the counts. Together they include all 15 apps' test packages.

Other checks: `manage.py check` no issues; `makemigrations --check --dry-run` no changes; `migrate
--check` clean on development; `verify_frontend_css.py` 41 modules, 527 selector pairs; `git diff
--check` clean.

## Browser verification

Headless Microsoft Edge 156 against `runserver`, with a throwaway PostgreSQL 18 database, since removed,
and sample data made through the project's services. 40 page states at 320, 375, 390, 768, 1024 and
1440px: 240 combinations. The development database was not used. Screenshots were inspected by eye for
a sample of states, including populated, empty, pending, rejected and suspended ones.

| Measure (of 240) | Before | After |
|---|---|---|
| Horizontal page overflow | 0 | 0 |
| Combinations with low text contrast (automated, selected elements) | 6 | 0 |
| Page states with uncaptioned tables (of 40) | 7 | 0 |
| Pages with tables scrolling at 390px | 6 | 1 (funding) |
| Visible Menu buttons at 320 to 768px | 2 | 1 |
| Keyboard measurements with an invisible focus outline | 6 | 0 |

After the changes: 0 CSP violations, 0 inline styles, one `h1` per page starting the heading order, no
skipped heading levels, every control labelled, every scrolling table keyboard-focusable. At 390px on
every page, the menu starts closed, opens, closes with Escape and returns focus to the button. At
1024px, all 1,351 Tab stops showed a focus outline. The one logged error is `/favicon.ico` returning
404, unrelated to this phase.

This is not an accessibility certification.

**Not verified:**

- Screen readers, forced-colors mode, 200% zoom, Firefox, Safari and real phones. Narrow widths were
  desktop emulation.
- Pages with JavaScript switched off.
- The open state of the team confirmation in a browser. Tests cover its markup, and it uses native
  `details`.
- Completed and archived campaigns, a form with visible errors, and real Bunny playback, in a browser.
  The preview showed the demo sample message.
- The production configuration: this ran in debug mode with sandbox on.
- Swahili wording. The repository tracks no translation catalogues.

## Known limitations

- Reports filter by date only. There is no campaign selector or export, because the backend has none.
- On phones, reports show verified views only; a note points to the campaign for other figures.
- Funding keeps two form panels and its existing scrolling tables on phones.
- Verified identity fields repeat their help line four times. The text comes from the form, and it stays
  because it is tied to each field.
- Eligibility messages and "fewer than N" are English strings from the services, outside translation.
- Dates use the locale's short format, which can be ambiguous in tables.

## Remaining UI-08 and UI-07 tasks

- UI-07: the console still uses `campaign_report`, `campaign_performance` and many cards, and its
  overview shows raw codes such as `worker_heartbeat_stale`.
- UI-08: screen-reader and real-device review, the items under "Not verified", human Swahili review, and
  a favicon.
