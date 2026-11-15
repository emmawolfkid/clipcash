# REG-04 username-first member onboarding

> **Superseded by PRD-01** (`prd_01_email_first_authentication.md`): sign-up no longer asks for a username, email
> sign-ups stay PENDING until the emailed link activates them, and the "add a username" page was removed. Kept from
> REG-04: `email_ownership_required` (migration 0006), email sign-in only once confirmed, owner-only confirmation for
> accounts with another identifier, and confirmation links that survive signing in again. The text below describes
> REG-04 as it was; the original files are kept in `ClipCash-releases/reg04-backup-2026-11-15/`.

Local and uncommitted on `port/mariadb-schema`, on top of the deployed REG-03 commit `038267a`. The main
worktree is not edited. Test records are fictional.

## Sign-up

* Required fields: First name, Last name, Username, Password (and its confirmation). The username rules and
  the 8-character member password policy (a letter and a number, spaces allowed, Django's checks) are as in
  REG-03.
* Contact choice: Email or Mobile number (Email selected by default). Only the chosen contact is required
  and stored. The other box is ignored, and it is hidden when JavaScript runs.
* Either choice signs the member in and opens the dashboard straight away. Sign-up is one transaction
  (`services.register_member`) and creates one ACTIVE member. Duplicate numbers, emails and usernames are
  refused on the form, normalized as before. Throttles: per IP, per username and per contact.
* Email sign-up still queues the confirmation link. Until it is confirmed, the dashboard shows "Confirm your
  email address" with **Resend link** and **Wrong address? Change it**, which uses the existing email-change
  flow. The profile page offers "Send a new link".
* A mobile number stays `phone_verified=False`. No sign-up path marks a contact verified.

## Sign-in and recovery on the new path

New accounts carry `email_ownership_required=True` (migration `accounts 0006`). Rows that already existed
get `False`, so their behaviour does not change.

* The username signs in at once. The mobile number also signs in, as it has since REG-02, while it stays
  unverified.
* The email address signs in only after it is confirmed (`User.email_signs_in`, enforced in
  `AccountStatusBackend`). Password reset already required a confirmed email, so it sends nothing before
  then.
* Only the signed-in account owner can confirm the address. A confirmation link opened while signed out, or
  by another account, shows "Sign in to confirm your email" (403) and changes nothing. Otherwise, whoever
  owned a mistyped inbox could confirm it and then take the account over through password reset.
* Signing in again does not spoil the emailed link. For these accounts the confirmation token leaves out the
  sign-in time, as the email-change token already does (`apps/accounts/tokens.py`). It is still bound to the
  account, password, address, status and confirmation state, so it works once and expires as before.
  Accounts from before REG-04 keep the exact old token, so links already sent stay valid. The real-browser
  check found this: before the fix, signing in on a second browser made the link "Link not valid".

## Existing accounts

* Existing members and staff sign in exactly as before. Legacy email accounts still confirm through the
  emailed link without signing in (flag `False`).
* A member waiting on **Confirm your email** sees "Continue with a username" (`accounts:add_username`,
  current password required). This sets a username on the SAME account (audit `account.username_added`),
  once. The email stays unverified and is not sent a reset link. The member reaches the dashboard and can
  sign in with the username or, as before, with the email address.
* Staff are unaffected: a username counts toward `identity_ready` for members only, so staff still need a
  confirmed email (or number) for the member areas. `add_username` is for members only.

## Membership

Plan choice and checkout are unchanged, and both new kinds of member reach them. Activation still needs
staff verification of a payment. Confirming an email address (or having one confirmed) never activates a
subscription. This is covered by a test.

## Tests and checks

See the REG-04 review report for the exact runs. The coverage is in `apps/accounts/tests/test_reg04_username_first.py`
plus the REG-03 module and the registration tests updated for the new form.
