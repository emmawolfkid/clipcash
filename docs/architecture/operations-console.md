# Operations Console (OPS-01)

The console at `/manage/` uses the approved member portal shell: navy top bar and sidebar, white cards,
blue and gold accents, a sidebar that collapses on wide screens (the choice is remembered) and becomes a
drawer on phones and tablets (`member-portal.css`, `member-nav.js`, plus `console.css` and
`console-nav.js` for console sections and search).

## Navigation

`apps/administration/navigation.py` defines the menu once:

| Group | Sections (pages) |
|---|---|
| Overview | Overview |
| Management | Members · Membership (plans) · Videos (all videos, upload, storage, categories, viewing review) · Advertising (campaigns, new campaign, advertisers) · Referrals (referrals, rules, budget) |
| Finance | Payments (verify payments, payment methods) · Rewards and funding (video rewards, reward rates, earning limits, campaign funding) · Wallets (wallets, wallet checks) · Withdrawals (requests, identity checks, payout destinations, settings) |
| Services and reports | Reports (reports, analytics) · Customer support · Notifications |
| System | Staff and permissions (staff and user accounts, roles and groups) · System settings (all settings, platform settings) · System administration (system health, security, audit logs, Django administration) |
| Testing tools | Demo rewards, sandbox payments, demo withdrawals and exceptions — listed only with `DJANGO_SIMULATION_TOOLS=True` |

* Every page lists only what the staff member may open (`console_links`, the same checks each page
  enforces). A section with no visible page is hidden.
* The current section opens automatically; others open on demand (native `<details>`, no script).
  Collapsed, the rail shows section icons whose labels are tooltips.
* `ROUTE_TO_ITEM` maps every console route to the menu entry it highlights; `test_navigation` fails
  when a new route is not placed. All existing URLs are unchanged.
* The search box filters the menu by English and Swahili names and keywords (every word must match);
  it only ever shows links already rendered for this staff member.

## Dashboard

`apps/administration/dashboard.py` (read-only): cards (members, active memberships, confirmed
membership payments, pending withdrawals), quick actions, "Needs attention" with counts that link to
filtered pages, a money summary with three separate real-money lines (confirmed membership income,
funded rewards owed to members, money paid out), recent activity, and short alerts (e.g. background
jobs not running). Each figure appears only with the permission of the page it links to. Sandbox and
demo records are never included. Technical detail is on **System health**.

## Language

One resolver (`apps/core/language.py`, ACC-01) chooses the language for every page, in this order:

1. Signed in with a saved choice: the account's `preferred_language`.
2. A choice made on this device: the language cookie set by the EN/SW switch.
3. No choice yet, staff (or the console sign-in page): `DJANGO_CONSOLE_DEFAULT_LANGUAGE` (default `sw`)
   on every page (console, member, advertiser and Django administration), so it never changes while
   navigating.
4. Everyone else: the browser's language, or the site configuration default when the browser sends none.

The EN/SW switch keeps Django's CSRF-protected `set_language` URL and also saves the choice on the
signed-in account. A saved choice is restored at the next sign-in on any device, and on a shared device
each person gets their own saved choice. A new member keeps the language chosen on the sign-up page.
Emails are written in the recipient's saved language (else the site default). Before ACC-01 the console,
the site default and the browser each decided separately, so an unsaved choice changed between pages.

## Testing tools

`DJANGO_SIMULATION_TOOLS` (default off) controls whether sandbox payments, demo rewards and demo
withdrawals appear in menus and dashboards (console, member and advertiser). Their pages and records are
kept and stay reachable directly; real-money switches are unaffected. Automated tests run with it on.

## Account header

Headers show the person's name (or "My account"), initials and a role label (Administrator, Staff,
Member, Member · Advertiser). The internal account ID, email address, phone number and username are not
shown.
