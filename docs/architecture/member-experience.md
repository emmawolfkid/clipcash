# Member experience (Phase 11)

Phase 11 adds a single member area that pulls together membership, sponsored
viewing, demo rewards, sandbox payments, the wallet, withdrawal availability,
the profile and activity. It adds no new money features and no migrations.

## Architecture

- **`apps/core/dashboard.py`** is a **read-only** view of the member's data, built from the domain services that own each area:
  - **Membership:** `memberships.access.check_access` and the member's pending subscription.
  - **Sponsored videos:** `viewing.services.available_campaigns`, the same pre-filter as `/watch/`. Its authoritative checks still run when a session starts.
  - **Viewing:** counts of the member's own sessions (verified and under review).
  - **Demo rewards:** one aggregate over the member's `DemoReward` rows.
  - **Wallet:** `wallet_services.wallet_eligibility`, `wallet_for` and `balances`, the ledger projection.
  - **Withdrawals:** `withdrawals.services.withdrawal_eligibility` plus a count of payout destinations.
  - **Payments:** counts of the member's sandbox intents, only when sandbox mode is active.

  It computes no balances, creates nothing and changes no state. A test checks that loading every member page writes no rows, audit events included, and never evaluates pending demo rewards.

- **Activity** (`member_activity`) merges **safe summaries** from the member's own domain records:

  | Source | What's shown |
  | --- | --- |
  | Subscriptions | Plan and status. |
  | Finalized watch sessions | Campaign and outcome. |
  | Demo rewards | Status, plus the amount marked "simulated". |
  | Sandbox payment intents | Reference and status. |
  | Wallet | "Opened", "placed under review", "lifted". |
  | Demo withdrawals | Reference and status. |
  | Account | Account created and email confirmed. |

  It never reads raw audit rows, reviewer reasons or identities, fingerprints or payloads. Each source is sliced to at most N rows, and the merged list is capped (6 on the dashboard, 50 on `/dashboard/activity/`).

## Pages

| Route | What it shows |
| --- | --- |
| `/dashboard/` | Summary cards (see below), sponsored videos for the member (top 3, from the existing listing), recent activity, latest library videos. |
| `/dashboard/activity/` | Activity feed with breadcrumbs. |
| `/accounts/profile/` | Now in the member layout: profile details (including whether the email is confirmed), account and membership status, password change and email reset (the existing flows), security guidance, and a **POST** sign-out. |
| `/memberships/mine/` | Unchanged text, plus a sandbox notice: simulated payments never activate a membership. |

The dashboard cards are:

- **Membership:** plan, status and expiry, or the pending choice.
- **Sponsored videos:** available campaigns, plus verified views and views under review.
- **Rewards (SIMULATED):** shown as "SIMULATED REWARDS — NOT WITHDRAWABLE".
- **Wallet:** only once a wallet exists, as "TZS 0 available (real ledger)".
- **Withdrawals:** "Not available", with the first plain-language reason and the number of destinations.
- **Payments (Sandbox):** the number of test payments.

**What the dashboard never shows:**

- a wallet figure before a wallet exists, or any figure built from demo or sandbox records;
- a "withdraw" button;
- earnings claims;
- staff links.

Wallet creation stays a POST on `/wallet/`.

## Navigation, responsiveness and accessibility

- **Grouped sidebar:** Home, Watch, Money and Account, with `aria-current="page"` and a visible inset marker on the current item, so it isn't shown by colour alone.
- **Mobile menu:** a "Menu" button (`aria-expanded`, `aria-controls`; Escape closes it) from `static/js/member-nav.js`. **Without JavaScript the menu stays visible.**
- **Touch targets** are at least 2.5–2.75 rem high on narrow screens.
- **No horizontal scrolling:** on narrow screens, wide tables scroll on their own instead of the page, and cards stack into one column.
- **Breadcrumbs** are a `nav` landmark (`aria-label="Breadcrumb"`) with `aria-current`.
- **Headings and labels:** every card is a `section` labelled by its heading. Status is always written as text (badges carry words), and simulated items carry a "Simulated" or "Sandbox" badge.
- **Code style:** no inline styles or scripts (the CSP allows none). Existing design tokens and focus-visible outlines are reused.

## Query performance

Each card uses a fixed number of queries: counts and aggregates, one-row
fetches, and `available_campaigns`, which is already a bulk pre-filter with
`select_related` and `prefetch_related`. Activity sources use `[:N]` slices
with `select_related` for campaign names. Full histories stay on their own
paginated pages.

A test checks that the dashboard's query count doesn't grow with the number
of rewards, sessions or campaigns. Only a fixed overhead is added once a
wallet exists.

## Security and privacy

- Every query is scoped to `request.user`, and other members' records never appear (tested).
- No staff, advertiser-organization or reviewer data is shown; for example, a wallet-restriction reason stays staff-only.
- No playback URLs appear in listings: links go to the viewing pages, which sign playback per session.
- GET requests change nothing (tested); every mutation is a POST with CSRF.

## Phase 12 boundary

- The administration dashboard can reuse the same pattern: a read-only summary module per domain, with bounded aggregates and no new calculations.
- Browser acceptance (desktop and mobile), screen-reader checks and contrast measurement are pending, and are planned for the review after Phase 12.
