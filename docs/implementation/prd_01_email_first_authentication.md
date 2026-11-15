# PRD-01: email-first authentication

Local and uncommitted on `port/mariadb-schema` (base `038267a`), adapting the uncommitted REG-04 work.
A copy of REG-04 as found is kept outside the repository in `ClipCash-releases/reg04-backup-2026-11-15/`.
Owner decision (15 Nov 2026): email delivery only; SMS is postponed. Test records are fictional.

## Behaviour

| | Email sign-up | Mobile sign-up |
|---|---|---|
| Fields | first name, last name, email, password ×2 | first name, last name, Tanzanian mobile, password ×2 |
| Account after sign-up | `PENDING`, not signed in, "Check your email" page | `ACTIVE`, signed in, dashboard |
| Activation | Opening the emailed link (GET shows a button, POST confirms) → `ACTIVE`, email verified. Never signs anyone in | — |
| Sign-in | Email + password, only after activation | Number (any format) + password |
| New paid membership | Yes, once active | **No** until an email is added and confirmed (`contact_unverified`) |
| Password reset | Confirmed email only | Only after adding and confirming an email; otherwise support |
| Duplicate identifier | Same "Check your email" page; the owner's inbox gets a notice (or a fresh link if still pending). Nothing is revealed | Refused on the form (instant sign-up cannot hide it; rate-limited) |

* No username is asked for. Existing username accounts still sign in with it.
* `phone_verified` is never set: there is no SMS code, provider or test shortcut (`apps/accounts/phone_verification.py`
  documents the contract for a future phase).
* The gate on paid memberships is `memberships.services.new_payment_eligibility`, applied when a plan is chosen
  (including renewals). It is **not** applied to staff approval of a payment already submitted, or to a plan choice
  made before PRD-01, so no money already paid is stranded.
* After a password change or reset, the confirmed address gets "Your ClipCash password was changed"; other sessions
  end (session hash covers the password).
* Removing the mobile number is refused while it is the only identifier that can sign in.

## Data and migrations

`accounts 0006_email_ownership_required` (from REG-04, help text updated): one boolean column, default `False`. Existing
rows keep `False` and behave exactly as before. No data migration, no trigger change, reversible.
No other migration.

## Production email (NetPoa)

Required settings (environment only, never in the repository):

```
DJANGO_EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
DJANGO_EMAIL_HOST / DJANGO_EMAIL_PORT (587 + DJANGO_EMAIL_USE_TLS=True, or 465 + DJANGO_EMAIL_USE_SSL=True)
DJANGO_EMAIL_HOST_USER / DJANGO_EMAIL_HOST_PASSWORD
DJANGO_DEFAULT_FROM_EMAIL=ClipCash <no-reply@<domain>>   (domain with SPF and DKIM for the provider)
DJANGO_PUBLIC_BASE_URL=https://<domain>                  (links never use localhost or the Host header)
```

`check --deploy` now refuses, in public production (restricted demo off): a non-delivering backend
(`notifications.E004`, previously only warning W001) and a placeholder sender (`notifications.E005`), in addition to
the existing E001–E003 (recording backends, TLS+SSL, remote SMTP without encryption) and `core.E009`
(`PUBLIC_BASE_URL` must be an https origin of an allowed host).

Delivery path: requests queue `OutboundEmail` rows in their transaction; the `run_jobs` worker sends them (up to 6
attempts, backoff 60 s → 1 h; permanent SMTP rejections stop at once; expired links are never sent). Account links are
signed at delivery but dated to the request, so a late retry never extends a link.

**Cron is mandatory**: without it no activation or reset email leaves. One entry, every minute:

```
* * * * * /bin/bash <app root>/scripts/netpoa/run_jobs_cron.sh <virtualenv python>
```

Confirm on the health page and with `manage.py jobs_status` that the worker heartbeat is recent.

## Production acceptance (owner-controlled mailbox; not done in this phase)

Passing tests and a configured SMTP are not evidence of delivery. Before announcing sign-up:

1. `manage.py check --deploy` shows no notifications.* or core.E009 error.
2. `manage.py email_diagnostics --connect` opens and closes an SMTP session (sends nothing).
3. `manage.py email_diagnostics --send-to <owner mailbox>` and confirm it arrives (inbox, not spam) within 2 minutes;
   check SPF/DKIM pass in the message headers.
4. Register with a second owner-controlled address: the "Check your email" page appears; signing in is refused.
5. The activation email arrives; its link starts with the production `https://` origin; opening it and pressing
   Confirm shows "Account activated"; the same link a second time shows "Link not valid".
6. Sign in with that address and password.
7. Request a password reset; the email arrives; the link works once; the new password signs in; the
   "password was changed" email arrives; another browser session is signed out.
8. Request a reset for an address that has no account: the same page is shown and nothing arrives.
9. Register with a mobile number: dashboard shows "Add a recovery email address"; choosing a plan is refused; add and
   confirm an email; choosing a plan then works.
10. Delete or disable the test accounts afterwards (staff console); record the date, commit and results.

## Existing accounts

* Email accounts (all accounts on the observed NetPoa build `fc35cf4`): unchanged sign-in, verification and reset.
* Legacy `PENDING` invitations (no password): still activated through the set-a-password link.
* Phone-only and username-only accounts (local REG-02/REG-03 data only): still sign in; cannot start a new paid
  membership until an email is confirmed; in-flight choices and submitted payments continue.
* REG-04 accounts with a username and an unconfirmed email (local only): the address is still confirmed only by the
  signed-in owner.

## Not in this phase

SMS verification and SMS recovery (future phase, owner budget decision); deployment; production settings.
