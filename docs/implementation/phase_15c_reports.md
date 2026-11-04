# Phase 15-C — Reports

Baseline: `d3a5197`. Approved decisions: D17 (eight reports, one permission each; advertisers see
only their own organization), D18 (CSV for authorized staff, audited, personal data minimised),
D12 (viewer-count threshold 5). Status: implemented and tested, **not committed**.

## Staff reports (`/manage/reports/`)

| Report | Permission (`reports.`) | Funds labels in the report |
|---|---|---|
| Members and memberships | `view_members_report` | none |
| Advertisers and campaigns | `view_advertising_report` | Demo budget (not money); Real money (locked, 0) |
| Simulated rewards | `view_rewards_report` | Simulated demo rewards (not money) |
| Sandbox payments | `view_payments_report` | Sandbox (test data, not money); Real money (locked, 0) |
| Demo withdrawals | `view_withdrawals_report` | Demo withdrawals (no money moves) |
| Operations | `view_operations_report` | none |
| Audit and security | `view_audit_report` | none |
| Customer support | `view_support_report` | none |

`reports.export_reports` is needed as well to export. Super Administrators hold every permission.
Permissions are granted per staff member or group; no role receives them automatically. The
`ReportAccess` model has no table and only carries these permissions.

The period is two inclusive dates in the site time zone (`SiteConfiguration.default_timezone`),
at most 366 days, defaulting to the last 30 days. Each section is marked "In this period" or
"As of today".

## Sources: no parallel ledger, no new balances

All figures are counts and sums over stored records, or the owner's own read service:
`administration.summary` (`_members`, `_wallets`, `_withdrawals`, `_jobs`),
`advertising.campaign_summary`, `viewing.PERFORMANCE_FIGURES`, and `payments.sandbox_balances`,
shown as returned. Each money-like section names one funds type and no figure is ever added across
types: there is no grand total. No wallet, payment, withdrawal or reward code changed.

## CSV export

`POST /manage/reports/<slug>/export/` (CSRF-protected), both permissions required, 20 per hour per
user. Long format, one figure per line: section, funds type, period, as of, row, measure, value.

* Audited: `reports.exported` (report, period, row count); refusals: `reports.action.denied`.
* Aggregates only: no member or staff identity. Organization and campaign names are business data.
* Text a spreadsheet would run as a formula is neutralised with a leading apostrophe.
* Per-campaign counts below the threshold read "fewer than 5", as do the simulated amounts that
  could reveal them. Staff still see exact figures on screen.
* `Cache-Control: no-store`.

## Advertiser reports (`/advertisers/<organization>/reports/`)

A "Reports" tab for team members: totals and per-campaign results for a period, limited to that
organization (another organization is "not found"). There is no advertiser CSV export.

## D12 privacy threshold

`DJANGO_ADVERTISING_REPORT_MIN_COUNT` now defaults to 5. Non-zero counts below it read "fewer than
5" on every advertiser page: organization overview, campaign page and the Reports tab. Zero stays
"0". The campaign page also hides the simulated amount used and remaining, because amount divided by
reward per view gives the hidden count.

## Navigation

Console sidebar: "Reports" group, shown only with at least one report permission. Advertiser tabs:
"Reports". Both use existing components; no new stylesheet was needed.

## Migrations

| Migration | Change |
|---|---|
| `reports/0001_initial` | `ReportAccess` (no table) and its 9 permissions |
| `audit/0014_reports_category` | Adds REPORTS to the audit category constraint |

## Limitations

* The threshold limits direct disclosure only. Comparing a total with other rows, or overlapping
  date ranges, can narrow a hidden count down.
* Tables with per-campaign or per-account rows stop at 100 rows on screen and in exports, and say so.
* Exports are capped per user but have no second approver.
* Analytics (trends, funnels) belongs to Phase 15-D.
