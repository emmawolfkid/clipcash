# Phase 17-A — Account self-service

Baseline: `dfbbf46`. Implementation committed locally as `bd4cd49`. Stage A verification (5 November
2026) ran on top of that commit; its corrections are listed under "Stage A verification" and were
**uncommitted** when this was written. Not deployed. CC-013 stays **open** (see "Remaining CC-013 work").

## What already existed (kept)

Profile view, password change requiring the current password (ends other sessions, audited),
password reset, email confirmation, sign-out, and links to the account pages from the top
navigation and the member sidebar.

## What this phase adds

| Page | URL (under `/accounts/`) | Notes |
|---|---|---|
| Profile and security hub | `` (existing) | Adds edit links, last sign-in, a waiting email change, recent security activity |
| Edit profile | `profile/edit/` | First name, last name, phone only |
| Change email address | `email-change/` | Needs the current password |
| Cancel a waiting change | `email-change/cancel/` | POST only |
| Confirm a change | `email-change/<id>/<token>/` | From the email; GET shows a button, POST confirms |

The console sidebar also gains a "My account" link. Advertisers are ordinary member accounts
in organizations, so they use the same pages.

## Email change

1. The owner enters the new address and current password.
2. A `PendingEmailChange` row is written and the **sign-in address does not change**. A link goes to
   the new address and a warning (with no link and without the new address) to the current one.
3. Opening the link changes nothing. Pressing the button swaps the address, keeps it confirmed, ends
   the row and tells the previous address.

Why the address only moves on confirmation: it is the login, and advertiser invitations and the
demo allowlist trust it. The account is never left with an unproved address.

- **Link:** valid 24 hours, single use, signed at delivery and never stored. It stops working on a
  new request, cancellation, a password change or reset, a change of address or status. It survives
  a new sign-in, because the owner may have to sign in elsewhere to read the new inbox.
- **Enumeration:** an address already in use gets the same response and the same "waiting" display,
  but no link. If it is taken after the request, the link is refused as invalid.
- **Restricted demo:** a demo account using the verification waiver cannot change its address (it
  signs in because of that address). The whole feature closes when the `dummy` email backend is set,
  since the link could never arrive. Confirmed accounts are unaffected.

## Rate limits

| Operation | Limit |
|---|---|
| Wrong current password (password and email forms share it) | 5 / 15 min per account; a correct password resets it |
| Email change requests | 5 / hour per account; 3 / hour per requested address; 20 / hour per IP |
| Changing the phone number | 10 attempts / hour per account |

The per-address limit is separate from the password-reset one, so nobody can use up another
person's reset allowance by naming their address.

## Audit events

`account.profile_updated` (field names only), `account.email_change.requested` / `.denied` /
`.cancelled`, `account.email_changed` (keyed hashes of the addresses), and
`auth.current_password.failed`. Values, passwords and tokens are not recorded.

The hub shows the owner a label and time for their own recent events, and no IP details.

## Deliberately not included

Role changes, account deletion and payout or wallet changes. A phone number here is contact
information: it is not verified by SMS and does not affect payout destinations.

## Known limits

> **RG-03 update:** the first, sixth and eighth items below are resolved: other sessions now end on an
> email change, confirmation requires the signed-in owner and a recent password, and the link's token moves
> into the session (never posted, never in a later URL) with email-change paths redacted from logs. See
> [rg_03_security_hardening.md](rg_03_security_hardening.md). The list is kept as originally written.

- Other signed-in sessions stay signed in after an email change (only a password change ends them).
  Requesting a change needs the password and warns the current address.
- A duplicate phone number is refused, which does reveal that it is in use. The message names
  nobody and attempts are limited.
- An account that loses access to its current inbox needs an administrator: unverified accounts
  cannot reach these pages.
- An expired `PendingEmailChange` row, including its plain new address, stays until the owner's next
  request or password change. No job purges these rows.
- A staff change of the account's address or password in the admin ends the link but not the row, so
  the hub can show a dead change as waiting until it expires.
- Whoever opens a link can confirm it without signing in. Someone could therefore ask a person to
  confirm an address for an account that is not theirs. That account would then hold the address,
  including any advertiser invitations waiting for it. It takes a deliberate click on a page that
  says to continue only if you asked. Requiring the owner's sign-in would close this, but is a policy
  decision for the project owner.
- The previous address gets a notice but no link to undo a change. Recovery goes through support.
- The link token is in the URL. It is not stored in audit events or the outbox, but Django's
  `django.security.csrf` warning logs the path on a CSRF failure, and hosting proxies may log URLs.
- Rate limits check and then count, so simultaneous requests can pass a limit by a small margin.
- Failed confirmation attempts are not audited.
- The repository tracks no translation catalogues, so the new text is English only.

## Remaining CC-013 work

Register entry CC-013 also lists **account closure** and **data export**. Neither is implemented.
Closure and retention need decision D20 first. Do not mark CC-013 resolved until both are delivered
or formally waived.

## Stage A verification

### Corrections

| Change | Reason |
|---|---|
| `confirm_email_change` locks the account, then the request | Confirmed defect. On `bd4cd49`, confirming a link while changing a password raised PostgreSQL `deadlock detected`, and one side got a server error. Every other path already locked the account first |
| Warning on the confirm page | Reduces the risk of someone confirming a change they did not ask for |
| 5 tests added to `test_self_service.py` (64 to 69) | CSRF on confirmation, per-IP limit, invalid link hides the address, staff role, flags and groups kept, confirm-page warning |
| New `test_self_service_concurrency.py` (5 tests) | Real concurrent transactions. The module had none |

### Security review

| Area | Result |
|---|---|
| Address changes only on confirmation | Yes. Requesting and opening the link change nothing; POST with a valid token does |
| Expired, reused, forged, cross-account links | Refused with 400 and tested. Lifetime is dated to the request, so late delivery does not extend it |
| Takeover through the flow | None found. A request needs a session **and** the password; confirmation needs the new inbox. A password change or reset ends the link |
| Ownership and authorization | All signed-in actions use `request.user`. Posted ids are ignored. Unverified accounts are redirected |
| Privileges and money | The form and service write only name and phone. Role, flags and groups are kept. Financial, wallet and membership row counts are unchanged by the flow. The phone number is not used by payout code |
| CSRF | Enforced on edit, request, cancel, password change and confirmation |
| Sensitive data | Audit metadata holds field names and keyed hashes, and the request path is not recorded. `actor_email` holds the actor's address, as on every audit event |
| Concurrency | One link succeeds once. Two accounts cannot take one address. Concurrent requests leave one row. One phone number cannot reach two accounts |

### Test results

Run with the guarded `PostgreSQLTestRunner` on the existing `test_clipcash_db` (`--keepdb`), with no
other sessions on it beforehand. Command form: `python -B manage.py test <labels> --keepdb --noinput`.

| Run | Labels | Result |
|---|---|---|
| Baseline, before corrections | `apps.accounts apps.audit` | 392 tests in 131.288s, OK |
| Concurrency, before the fix | `apps.accounts.tests.test_self_service_concurrency` | 5 tests in 9.466s, **1 failure** (the deadlock) |
| 17-A modules, after the fix | `test_self_service`, `test_self_service_concurrency` | 74 tests in 23.649s, OK |
| Focused | `apps.accounts apps.audit apps.notifications` and `apps.core.tests` `test_design_system`, `test_public_auth_ui`, `test_member_viewing_ui`, `test_member_experience`, `test_homepage` | 516 tests in 118.762s, OK |
| Permissions and boundaries | `apps.administration apps.advertising apps.support apps.reports` | 215 tests in 105.628s, OK |
| Financial regressions | `apps.wallets apps.withdrawals apps.payments apps.rewards apps.memberships` | 260 tests in 102.148s, OK |
| Concurrency, repeated 3 times | `test_self_service_concurrency` | 5 tests each in 7.494s, 7.572s, 7.476s, OK |

Other checks: `manage.py check` no issues; `makemigrations --check --dry-run` no changes; `migrate
--check` clean on the development database; `verify_frontend_css.py` 41 modules, 445 selector pairs;
`git diff --check` clean.

**Not run:** the entire suite (`apps.videos`, `apps.viewing` and the remaining `apps.core` tests),
browser or visual checks, real SMTP delivery, and anything on Render. Templates were checked only
through test rendering.

## Migration and deployment

`accounts/0003_pending_email_change` depends on `accounts/0002_role_groups` and only creates
`accounts_pendingemailchange` (unique `user_id`, deferred foreign key to `accounts_user`, no triggers).
It does not change, drop or rewrite existing tables or rows. Its reverse would drop the table: go
forward only, as for every migration (see the runbook on rollback).

| Database | State |
|---|---|
| `clipcash_db` (development) | Applied 5 Nov 2026 16:51 +03:00, no waiting rows |
| `test_clipcash_db` | Applied 5 Nov 2026 15:28 +03:00, before Stage A; reused by the runs above |
| `test_clipcash_ui02_css_20261104`, `test_clipcash_ui03_20261104`, `test_clipcash_ui04_20261104` | Not applied and not touched. They are also behind on other migrations |
| Production | **Not applied.** State unknown from here |

Rehearsed on a throwaway local PostgreSQL 18 server, since removed: pre-0003 schema as a non-superuser
owner, 3 seeded users, scripts `01`–`04`, then `migrate` as `clipcash_migrator`. Results:

- The seeded `accounts_user` checksum was identical before and after.
- The table is owned by `clipcash_owner`.
- `clipcash_app` had SELECT, INSERT, UPDATE and DELETE through default privileges alone, and no
  TRUNCATE, REFERENCES or TRIGGER. Re-running `03` changed nothing and `04` passed.
- Request, cancel, confirm, replay and profile update all worked as `clipcash_app`.
- `clipcash_app` was refused `ALTER TABLE` and `TRUNCATE` on the new table.

Under role separation, run the migration as `clipcash_migrator` and then re-run
`03_grant_runtime.sql` (runbook §7). `04_verify_runtime_role.sql` has no probe specific to this table.
