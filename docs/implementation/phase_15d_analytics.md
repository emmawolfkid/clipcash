# Phase 15-D — Analytics

Baseline: `f4873b6`. Status: implemented and tested, **not committed**. This phase's scope, as
given by the owner, closes decision D19 (definition of "Analytics").

## What it is

A staff dashboard at `/manage/analytics/`: summary cards, trend charts and breakdowns for a chosen
period. Code: `apps/reports/analytics.py`, `templates/administration/analytics/`,
`static/css/pages/administration/analytics.css`.

| Section | Needs (`reports.`) | Content |
|---|---|---|
| Memberships | `view_members_report` | New members, activations, current memberships; trend; activations by source |
| Campaigns | `view_advertising_report` | Created, awaiting review, active; trend; by status |
| Verified viewing | `view_advertising_report` | Sessions, verified, rate, viewers, in review; trend; session-to-result steps; top 5 campaigns |
| Customer support | `view_support_report` | Opened, resolved, open, unassigned; trend; by category |
| Operations | `view_operations_report` | Failed jobs, emails queued and failed, queue status; trend |
| Financial categories | One permission per category (below) | Cards only, one panel per funds type |

## Access

No new permission and no migration: each section needs the permission of the report it summarises,
so analytics shows nothing a staff member could not already read. Staff holding none of these get
403, and the sidebar entry appears only for those who may open the page. Non-staff, advertisers
included, are sent to the admin login; there is no advertiser analytics. Staff figures are exact
across organizations, like the staff reports. Advertiser pages and exports keep the D12 masking.

## Financial separation

| Panel | Label | Needs |
|---|---|---|
| Simulated rewards | Simulated demo rewards (not money) | `view_rewards_report` |
| Sandbox payments | Sandbox (test data, not money) | `view_payments_report` |
| Demo withdrawals | Demo withdrawals (no money moves) | `view_withdrawals_report` |
| Demo budgets | Demo budget (not money) | `view_advertising_report` |
| Real money | Real money (locked, 0) | `view_advertising_report` or `view_payments_report` |

Each panel holds one funds type and nothing is added across them: there is no financial total. The
viewing section's session-to-result steps include simulated rewards only as a count, and only with
`view_rewards_report`. No wallet, payment, withdrawal or reward code changed.

## Sources

The Reports period (`services.period_from`: site time zone, inclusive dates, 366 days at most) and
Reports helpers (`_by_choice`, `_campaign_viewing`, `reward_totals`), plus `administration.summary`,
`advertising.campaign_summary` and `viewing.PERFORMANCE_FIGURES`. `reward_totals` was extracted from
the rewards report so both use one definition. Pages are read-only and create no rows.

## Charts

Server-rendered SVG with precomputed geometry: no JavaScript, chart library, external service or
inline CSS. Buckets are days up to 45 days, weeks up to 200, otherwise months, and zero-filled. Weeks
start on Monday, so the first bucket's label can fall before the period starts; it still counts only
records inside the period. Each chart has a "Show the figures" table, and a period with no activity
shows an empty state.

## Migrations

None.

## Limitations

* No comparison with the previous period, and no saved views or analytics export.
* The funnel follows sessions started in the period; its reward count is for those same sessions, so
  it can differ from the Simulated rewards panel, which counts by decision date.
* Layout was checked with sample data at 1280px and 390px in headless Edge, not with real traffic.
