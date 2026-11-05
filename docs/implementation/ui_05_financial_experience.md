# UI-05 — Membership and financial experience

Baseline: `19e802b`. Status: implemented and tested, **not committed**. Not deployed. The first UI-05
templates arrived in `dfbbf46` without a document, tests or recorded results; this phase verified and
completed them. Final visual and accessibility acceptance stays with UI-08.

Scope: frontend presentation only. No model, migration, posting, payment, reward, withdrawal
execution, authentication, eligibility, audit or anti-fraud code changed. Real money stays locked.

## Genuine gaps found

Measured in Edge on the code as committed, except for the items marked "code review".

| Gap | Evidence | Fix |
|---|---|---|
| Sandbox and demo notices looked like ordinary notices | Computed background matched `.notice`: the base rule came later in `alerts.css` and won | Variants moved after the base rule |
| Badge variants lost their border and background | Same cause in `badges.css` | `.badge` moved first |
| Muted badges failed AA contrast | 4.33:1 on 30 page and width combinations | White background gives 4.89:1 |
| Page overflowed sideways | `destinations` at 320, 375, 390px; `funding` at 320px | `.table-scroll` and `.table` now contain the absolutely positioned hidden "Actions" heading |
| Members saw stored codes | `DEMO_UNDER_REVIEW`, `review_started` on the demo withdrawal page | Translated status labels |
| Complimentary access looked charged (code review) | History showed the plan price although `amount_paid` is 0 | "Complimentary, no payment"; detail separates listed price from amount paid |
| Funding page used raw amounts and had no captions | `TZS 75000000` style values (code review); 2 uncaptioned tables | `tzs` filter, captions, shared amount classes |
| No demo status badge styles (code review) | 10 of 11 demo withdrawal statuses had no rule | Added. Simulated "paid" stays neutral, never green |
| Rewards had no verified-view reference | No link to the session | "View session" per record, scoped to the member by the existing view |
| Wallet transaction lacked context (code review) | Reference, type and amount only | Adds date, direction and balance type |
| Identical amount rules (code review) | Three page rules under different names | One shared `.financial-amount--large`; two emptied modules removed |
| Pending page contradicted itself | "See plans" prompt shown above a waiting choice | Prompt replaced while a choice is waiting |
| Forms were cramped; references broke mid-value | Seen in screenshots | `.financial-form`, `.financial-reference` |

## What changed by area

- **Memberships:** consistent titles; panels for renewal and waiting choices; explanations for ended,
  cancelled, suspended and pending records; "Kept until" date; sign-in action styled as a button;
  repeated duration sentence removed. Prices, dates and statuses still come only from the records.
- **Rewards:** verified-view link, a Demo badge on each simulated amount, and an empty state pointing to
  sponsored videos. Reversed and not-eligible records stay visible.
- **Wallet:** direction column, transaction details, and the restriction announced to screen readers.
  The private restriction reason stays hidden. No money-movement action exists.
- **Payments:** an honest sentence for every intent status. `funding.html`, the template the earlier
  work left untouched, now uses formatted amounts and captions. Forms, tokens and routes are unchanged.
- **Withdrawals:** readable demo history with a created date, a visible preview label, a formatted
  preview amount, help text linked to the number fields, and "Verification" replacing the "Check"
  heading. Numbers stay masked.

## Files

| Kind | Files |
|---|---|
| Templates | `memberships/` my_membership, plan_detail, plan_list, subscription_detail; `payments/` funding, history, intent; `rewards/history`; `wallets/` wallet, transaction; `withdrawals/` demo, destinations, overview |
| Shared CSS | `components/` alerts, badges, financial, tables |
| Page CSS | `pages/withdrawals/demo.css` edited; `pages/payments/intent.css` and `pages/wallets/transaction.css` removed |
| View | `apps/withdrawals/views.py`: `DemoDetailView` adds a display label to each decision. Read-only |
| Tests and tools | `apps/core/tests/test_financial_ui.py` (32 tests), `scripts/run_ui05_regressions.py` |

The shared CSS fixes reach every page that uses these notices, badges and tables, including staff and
advertiser pages. Those pages' suites pass, but their appearance was not reviewed here.

## Preserved

CSRF tokens, form fields, routes, permission mixins, member and organization scoping (other parties
still get 404), `SandboxRequiredMixin` (payment pages return 404 when sandbox is off), masking, the
TZS 0 wallet lock, and all five financial categories, which are still never added together.

## Test results

Guarded runner, `test_clipcash_db`, `keepdb=True`, initially idle. No database was created or removed.

| Run | Command | Result |
|---|---|---|
| UI-05 and frontend regressions | `python -B scripts/run_ui05_regressions.py --database test_clipcash_db` | **872 tests in 450.849s, OK** |
| Shared-component consumers | `python -B manage.py test apps.administration apps.advertising apps.audit apps.notifications apps.support apps.reports apps.core --keepdb --noinput` | **530 tests in 262.729s, OK** |

The two runs overlap, so do not add the counts. Together they include every app's test package.

Other checks: `manage.py check` no issues; `makemigrations --check --dry-run` no changes; `migrate
--check` clean on development; `verify_frontend_css.py` 39 modules, 459 selector pairs; `git diff
--check` clean.

## Browser verification

**Performed:** headless Microsoft Edge 156 against `runserver`, using a throwaway PostgreSQL 18 database
and sample data made through the project's services. 32 page states at 320, 375, 390, 768, 1024 and
1440px: 192 combinations. The development database was not used. Screenshots were inspected by eye for
a sample of states and widths, not all of them.

| Measure (of 192) | Before | After |
|---|---|---|
| Horizontal page overflow | 4 | 0 |
| Combinations with low text contrast (automated, selected elements) | 30 | 0 |
| Combinations showing raw status codes | 12 | 0 |
| Uncaptioned tables | 12 | 0 |

After the changes: 0 CSP violations, 0 inline styles, exactly one `h1` per page, every control labelled,
and every scrolling table keyboard-focusable. At 1024px, 794 Tab stops across 32 pages all showed a
visible focus outline. The one logged error is `/favicon.ico` returning 404, unrelated to this phase.

**Not verified:**

- Screen readers, forced-colors mode, 200% zoom, and Firefox, Safari or real phones. Narrow widths were
  desktop emulation.
- The wallet transaction page in a browser: the database refuses these rows, so it has only a template
  test on an unsaved record.
- Statuses without sample data, such as failed or refunded intents and most demo withdrawal outcomes.
- The production configuration: this ran in debug mode with sandbox on.
- Swahili wording. The repository tracks no translation catalogues, so hooks and rendering are tested
  but the text stays English.

## Remaining limitations

- Two "Menu" buttons stack at 768px and below (site header and member sidebar). This comes from the
  shared layouts, so it was left alone and should be decided in UI-08.
- Wide financial tables scroll sideways on phones instead of becoming cards, keeping real table
  semantics. Columns such as Status can start off-screen.
- On small screens the destinations table has tall rows because of its narrow actions column.
- Reward reasons and some server messages are English strings from the services, outside translation.
- Ledger descriptions are not shown to members: they are internal text, and no member rows can exist
  yet. The transaction page shows the category label instead.
- Removing a payout destination has no confirmation step, as before.
- `Settled` keeps a green badge on sandbox pages, always beside sandbox labels.
