# Administration dashboard and operations control centre (Phase 12)

The staff console at `/manage/` gathers every ClipCash area into one place:
an executive overview, operational health, a member directory and a security
summary. Navigation is grouped and filtered by permission.

**The console adds no new actions.** Every change still goes through the
existing domain pages and services, and there are no migrations.

## Data sources

`apps/administration/summary.py` is a read-only module:

| Section | Source (owning area) | Shown to |
| --- | --- | --- |
| Members | `User` aggregates (total, active, email-confirmed, suspended/disabled); `Subscription` status counts and current memberships | `can.members`: Super Administrator or `accounts.view_user` |
| Advertising | Organization verification counts; campaign status counts; demo budget of approved campaigns; funded campaigns (always 0) | `advertising.review_advertisers` / `review_campaigns` |
| Videos | Publication and processing counts; uploads stuck past `VIDEO_UPLOAD_STALE_HOURS` | Video console permissions (Content Managers included) |
| Viewing | Outcome counts, pending reviews, revocations, idle sessions not yet closed | `viewing.review_viewing` |
| Demo rewards | Recorded, reversed and not-eligible counts; simulated total; policy drafts; top five demo budgets used | Rewards console permissions |
| Payments (sandbox) | Intent status and purpose counts; open exceptions, pending refunds, open disputes, membership previews | `payments.view_payments` **and** sandbox mode active |
| Wallets | Wallet count, restricted wallets, open findings, unexpected-balance findings | `wallets.view_wallets` |
| Withdrawals (demo) | Status counts, review queue, unknown payout statuses, exceptions | `withdrawals.view_withdrawals` |
| Background jobs | `apps.core.jobs.services.queue_health()` | Super Administrators |

**Rules:**

- **Permission-aware:** a section is only *computed* when the staff member holds the area's own permission (`permissions()` reuses `console_links()`). Missing permissions mean the section is omitted, never shown with partial data.
- **Read-only:** console GET pages create no rows, audit events, jobs or reward decisions, and make **no external calls**. Tests check all of this, including with Bunny's HTTP client patched to fail if it's ever called.
- **Separate money semantics:** sandbox payments, simulated demo rewards and real wallet figures each have their own section and labels, and are never added together. Demo campaign budgets are labelled "configuration, not funding".

## Operational health (`/manage/health/`)

Each check reports one of four statuses:

| Status | Meaning |
| --- | --- |
| **Healthy** | The stored data shows no problems. |
| **Attention required** | Failures, backlogs or open items exist. |
| **Not configured** | The service is off or still in a development setup. |
| **Not checked** | ClipCash can't know from stored data alone. External services are **never** contacted from this page, so a configured Bunny Stream or mail server shows Not checked, not Healthy. |

| Check | Rule |
| --- | --- |
| Background jobs | No worker heartbeat yet: Not checked. `queue_health` problems or failures in the last 24 h: Attention. Otherwise Healthy. |
| Email delivery | Console or locmem backend: Not configured. Failures in 24 h, or emails pending over 1 h: Attention. Otherwise Not checked. |
| Video provider | Demo provider, or missing Bunny settings (shown as a count, never names or values): Not configured. Stuck or failed processing: Attention. Otherwise Not checked. |
| Viewing sessions | Pending reviews or idle unclosed sessions: Attention. |
| Sandbox payments | Exceptions, refunds awaiting a second approver, or disputes: Attention. Payments disabled: Not configured. |
| Wallet integrity | Open reconciliation findings: Attention. |
| Demo payouts | Unknown payout statuses or exceptions: Attention. |
| Advertiser and campaign reviews | Pending reviews: Attention. |
| Demo reward policies | Drafts awaiting approval: Attention. |

A staff member only sees the checks for areas they can access. Content
Managers, for example, see only the video provider check.

## Member operations (`/manage/members/`)

- **Directory:** search by email, paginated. Each row shows account status, email confirmation, the latest membership, and wallet status (that column only for `wallets.view_wallets`). One query with subqueries, so no N+1.
- **Member detail:** account, current membership, recent memberships, the wallet link, and the Phase 11 **safe activity summaries**. Reviewer reasons and staff identities are never shown.
- **Actions** link to the existing reviewed workflows (Django admin user change, which needs `accounts.change_user`). There are no new activation, payment or status flows, and sandbox payments never activate memberships.

## Security and audit (`/manage/security/`)

Requires `audit.view_auditevent`. It covers the last 24 hours, using indexed
time filters:

- event counts by outcome;
- denials by area;
- refused financial actions (payments, wallets, withdrawals, rewards, ledger);
- the 20 most recent staff actions and events.

It shows **no event metadata**. The full audit log stays in the read-only
Django admin, and audit records are never edited.

## Navigation

The sidebar is grouped. Each link appears only when the user holds its
permission, and every target re-checks that permission on the server.

| Group | Links |
| --- | --- |
| Overview | Overview, Operational health |
| Members | Member directory, User accounts (admin), Membership plans |
| Advertising | Advertisers, Campaigns |
| Videos and viewing | Video management, Categories, Viewing review |
| Financial simulation | Demo rewards, Payments (sandbox), Wallets, Withdrawals (demo) |
| Operations | System settings |
| Security and audit | Security summary, Audit logs |

- **Current page:** `aria-current="page"` plus a visible inset marker.
- **Breadcrumbs** on the health, members and security pages.
- **Mobile menu toggle:** shared with the member layout (`static/js/member-nav.js`, now generic). Without JavaScript the menu stays visible.
- **Tables** scroll on their own on narrow screens, and status badges always carry text.
- **No inline CSS or JS.**
- A test follows every console link a Super Administrator can see, and expects a 200 response.

## Permission matrix (verified)

| Role | Sees |
| --- | --- |
| Super Administrator | All sections and links (holds every permission). Separation of duties stays inside the services: two-person policy, refund, restriction and approval rules, and no self-approval. |
| Content Manager | Video sections, the video health check and video links only. Members, the security summary and member detail return 403; financial and advertiser areas are hidden and re-checked server-side. |
| Members, advertisers (non-staff) | Redirected to the admin login for every console URL. Advertiser tenant isolation stays in `/advertisers/` and is unchanged. |

No permission was broadened to fill a card.

**One change from Phase 04:** the old overview showed every count to any
staff account, so a Content Manager could see member and financial figures.
Each figure is now behind its owning permission.

## Query performance

- Every section uses a fixed number of aggregates and counts, small `[:5]` lists, or time-bounded queries.
- The member directory uses subqueries for membership and wallet status, and is paginated.
- `queue_health()` is the existing bounded jobs snapshot.
- A test checks that the overview's query count doesn't grow with more rewards, sessions and members.

## Financial boundaries (unchanged)

The console adds no credit, debit, transfer, funding, activation or payout
controls. Tests confirm the database locks are still installed:

| Trigger | Locks |
| --- | --- |
| `a_wallets_block_member_postings`, `wallets_transactions_locked` | Wallet postings (Phase 09) |
| `withdrawals_request_locked` | Real withdrawals (Phase 10) |
| `advertising_campaign_guard` | Campaign state, alongside the `adv_campaign_funding_locked` funding constraint (Phase 05) |

## Known limitations

- Health checks use stored data only. Real reachability of Bunny Stream, the mail server and future providers has to be checked outside the app, for example through the deploy checklist and uptime monitoring.
- The security summary covers 24 hours; older events are in the Django admin audit log.
- Member search is by email only.
- Reports, the old "Coming soon" item, is now off the roadmap list for Phase 12.

## Browser acceptance requirements (after Phase 12)

1. Log in as a Super Administrator, a Content Manager, a member, and an advertiser owner. Each should see exactly the sections in the permission matrix.
2. Check desktop and mobile widths (360 px, 768 px, 1280 px): the menu toggle, no horizontal page scroll, readable cards and tables.
3. Check keyboard-only navigation: visible focus, Escape closes the menu, the skip link works.
4. Run a screen reader over the landmarks, breadcrumbs and badge text; measure colour contrast for the badges.
5. Walk through every review workflow from the console: advertiser, campaign, viewing, policy (two people), refund (two people), wallet restriction lift (two people), demo withdrawal (creator, two approvers, a different executor).
6. Confirm health statuses with a real worker (`run_jobs`) and the real email backend on staging.

## Phase 12 completion criteria

- [x] Permission-aware read-only overview, health, members and security pages.
- [x] No writes, audit events or external calls on GET (tested).
- [x] Grouped navigation with only authorized links; every link resolves (tested).
- [x] Financial locks intact; sandbox, simulated and real figures kept separate.
- [x] Full PostgreSQL regression suite green.
- [ ] Browser acceptance (above): pending, run by the project owner.
