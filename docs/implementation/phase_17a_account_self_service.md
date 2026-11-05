# Phase 17-A — Account self-service

Baseline: `dfbbf46`. Status: implemented and tested, **not committed**.

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

- Other signed-in sessions stay signed in after an email change (only a password change ends them).
  Requesting a change needs the password and warns the current address.
- A duplicate phone number is refused, which does reveal that it is in use. The message names
  nobody and attempts are limited.
- An account that loses access to its current inbox needs an administrator: unverified accounts
  cannot reach these pages.
- An expired `PendingEmailChange` row stays until the owner's next request or password change.

## Deployment

Migration `accounts/0003_pending_email_change` adds one table with no changes to existing rows.
Under role separation, run the migration as `clipcash_migrator`. `clipcash_app` receives access
through default privileges, and re-running `03_grant_runtime.sql` after it is the documented step.
