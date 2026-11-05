# UI-07 — Administration and operations workspace

Baseline: `96dd28d`. Status: implemented and tested, **not committed**. Not deployed. Final visual and
accessibility acceptance stays with UI-08.

Scope: presentation of the existing console. No migration, permission, approval, publishing, anti-fraud,
ledger, reconciliation, reward, withdrawal, privacy-threshold or audit behaviour changed. Real money
stays locked.

## Pages inventoried

All 39 routes in `apps/administration/urls.py` and the 42 templates in `templates/administration/`,
covering overview and health, members and complimentary grants, security, videos and categories,
advertisers and campaigns, viewing review, demo rewards and policies, sandbox payments, wallets, demo
withdrawals, support, reports and analytics. Also `templates/support/_conversation.html` and the staff
view of the notification inbox.

There is no staff-wide notification history page, so none was invented. Staff read their own inbox. Its
template is protected by earlier phases and was not edited; a test confirms it still renders for staff.

## Genuine issues identified

| Issue | Evidence | Fix |
|---|---|---|
| Wall of cards | Overview 9, campaign pages 12, advertiser pages 15, analytics 48 | Metric rows, tables, definition lists. 154 cards across sampled pages became 6 |
| Open work was buried | Pending items sat inside card sentences | "Needs attention" table of real counts with working links |
| Raw codes visible to staff | 9 page states: `worker_heartbeat_stale`, fraud flags, `DEMO_UNDER_REVIEW`, reason codes | Display labels in `apps/administration/labels.py`. Stored values unchanged |
| Unformatted amounts | `TZS {{ amount }}` throughout | `tzs` filter; analytics figures grouped |
| Uncaptioned tables | 26 of 48 page states | Every console table has a caption |
| Tables scrolling sideways on phones | 24 page states at 390px | 11 now. Queues hide secondary columns; action and financial tables still scroll, with keyboard access |
| Invalid markup | `<form>` inside `<p>` for refunds and disputes | `<div>` records |
| Console menu taller than the screen | Lower links unreachable until the end of the page | Menu scrolls on its own and starts with the current page in view |
| Leftover template conditions | `{% if True %}` in the navigation | Removed; personal links moved to "Your account" |
| Small targets on analytics | 7 at 390px | "Show the figures" meets the touch target |

## Design changes

- **Overview:** one metric row of non-financial counts, a "Needs attention" table, and a "By area" table.
  Financial areas carry their own badges and are never added together.
- **Detail pages:** related sections sit side by side under a single rule, with no boxes. Permanent
  actions keep their confirmations.
- **Support:** conversation rows replace cards, and internal notes have a distinct amber treatment so
  they cannot be mistaken for customer replies.
- **Analytics:** charts sit on the page. The five financial categories deliberately keep their boxes,
  because their separation is the point.
- **Audit event names stay visible beside their labels** on the security page and in audit histories,
  because auditors search the log by them. Ledger account identifiers also stay as stored.

## Read-only Python

- `apps/administration/labels.py` and `templatetags/console_labels.py`: display labels and number grouping.
- `summary.py`: new `attention_items()`, built only from sections the staff member may already see, and
  readable job problems in the health detail. No calculation changed.
- `views.py`: `OverviewView` passes `attention` to the template.

## CSS structure

| Module | Role |
|---|---|
| `components/workspace.css` (new) | Page headings, metric rows, data tables. Moved from `layouts/advertiser.css`, because both workspaces now use them |
| `components/settings-groups.css` (new) | Settings groups and the confirmation disclosure. Moved from `pages/advertising/settings.css`, now removed |
| `layouts/admin.css` | Console-only rules, 103 lines |
| `layouts/advertiser.css` | Organization line and tabs only |
| `pages/administration/analytics.css` | Chart rules, adjusted |

42 modules, 548 selector pairs. No framework, inline CSS or inline JavaScript.

## Test results

Guarded runner, `test_clipcash_db`, `keepdb=True`, initially idle. No database was created or removed.

| Run | Command | Result |
|---|---|---|
| UI-07 and regressions | `python -B scripts/run_ui07_regressions.py --database test_clipcash_db` | **1,187 tests in 458.026s, OK** |
| Remaining core | `python -B manage.py test apps.core --keepdb --noinput` | **206 tests in 44.377s, OK** |

The two runs overlap, so do not add the counts. Together they include all 15 apps' test packages. The new
module `apps/administration/tests/test_console_ui.py` has 31 tests.

Other checks: `manage.py check` no issues; `makemigrations --check --dry-run` no changes; `migrate
--check` clean on development; `verify_frontend_css.py` passed; `git diff --check` clean.

**Existing tests changed:** only `apps/advertising/tests/test_workspace_ui.py`, because its CSS moved: one
path updated and one "page stylesheet is loaded" assertion removed.

**An earlier run failed and the test was right:** `apps.viewing.tests.test_web` expects the audit event
name on the session page, and my first version had replaced it. The templates were corrected and the test
was left unchanged.

## Browser verification

Headless Microsoft Edge 156 against `runserver`, with a throwaway PostgreSQL 18 database, since removed,
and sample data made through the project's services. 48 page states at 320, 375, 390, 768, 1024 and
1440px: 288 combinations, including a support agent, a report-only viewer and a content manager. The
development database was not used. Screenshots were inspected by eye for a sample of pages.

| Measure | Before | After |
|---|---|---|
| Cards across sampled console pages at 1024px | 154 | 6 (5 financial categories on analytics, 1 on the refused page below) |
| Page states with uncaptioned tables | 26 | 0 |
| Page states with tables scrolling at 390px | 24 | 11 |
| Page states showing raw codes | 9 | 2 (ledger account and audit names, kept on purpose) |
| Small touch targets at 390px | 7 | 0 |
| Horizontal page overflow | 0 | 0 |
| Visible Menu buttons at 320 to 768px | 1 | 1 |

After the changes: 0 CSP violations, 0 inline styles, one `h1` per page, no skipped heading levels, every
control labelled. The menu closed, opened, closed with Escape and returned focus on every page at 390px.
All 1,765 Tab stops at 1024px showed a focus outline. Logged errors: `/favicon.ico` 404 (unrelated) and
403 on the complimentary grant page, correctly closed in this mode. That page has a template test instead.

This is not an accessibility certification.

## Security safeguards

Permissions, CSRF, expected-revision and expected-status fields, four-eyes approvals, permanent-action
confirmations, masking and financial notices are preserved. Tests confirm that restricted roles get 403
and see no links to those areas, that stale approvals are refused, that customers never see internal
notes, and that view-only staff get no management forms.

## Remaining limitations

- 11 page states still scroll sideways at 390px: action and financial tables, whose columns are needed.
- Fallback labels for codes without a written label are generated from the code, so they stay in English.
- Provider summaries (`PAID 75000000`) show the provider's record as stored.
- The sidebar lists up to about 22 links. Grouped and permission-aware, but long.
- Staff reach their inbox through the member shell, not the console sidebar.
- Django admin pages linked from the console (user accounts, plans, settings, audit logs) were not restyled.
- Breadcrumbs appear only where views already supply them, while other pages use a back link.

## UI-08 acceptance items

Screen readers, forced-colors mode, 200% zoom, Firefox, Safari and real phones; JavaScript switched off;
the complimentary page, filled forms with errors and open disclosures in a browser; statuses without
sample data, such as disputes, refunds and uncertain payouts; real Bunny playback and upload; the
production configuration; human Swahili review; a favicon; a decision on the staff inbox shell.
