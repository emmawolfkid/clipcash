# PRD-03A: no mandatory verification at sign-up

Local and uncommitted on `port/mariadb-schema` (base `1e55f3f`). Supersedes the "email sign-up stays PENDING until its
link is opened" part of [PRD-01](prd_01_email_first_authentication.md). Test records are fictional.

## Behaviour

* **Sign-up** (email address, Tanzanian mobile number or username): the account is ACTIVE and signed in at once,
  then lands on the dashboard. Nothing is emailed. The identifier is stored unconfirmed (`email_verified` and
  `phone_verified` stay False). Identifiers stay unique in any letter case or number format; an identifier that is
  taken is reported on the form (instant sign-up cannot hide it). The per-IP and per-identifier rate limits and the
  password rules are unchanged.
* **Member pages** (dashboard, profile, plans, library, viewing list, advertiser home) open without any
  confirmation. Staff accounts keep the old rule (confirmed email or a mobile number) before staff or member pages.
  `access.account_ready` decides page access; `identity_ready` is unchanged and still guards the financial and
  identity-sensitive services.
* **Still requiring a confirmed email address** (unchanged financial restrictions):
  * choosing a plan or renewing (`new_payment_eligibility`; email accounts: `email_unverified`, phone/username
    accounts: `contact_unverified`); the plan page explains it with a "Send confirmation link" or "Add an email"
    action;
  * wallets, payout destinations and advertiser actions for accounts whose only identifier is an unconfirmed email
    (`member_eligibility`, as before);
  * advertiser invitations: only an account that has CONFIRMED the invited address sees, accepts or is notified of
    an invitation (new; sign-up no longer confirms addresses);
  * email copies of notifications and the "password changed" notice go only to confirmed addresses.
* **Confirming an email address** is optional and on request: "Send confirmation link" on the profile and plan
  pages. On accounts created by public sign-up only the signed-in owner can open the link.
* **Password reset** goes to:
  * a confirmed address; or
  * the address an email sign-up uses as its only identifier (`User.email_recovers_account`).

  Never to a mobile number or a username, and never to an unconfirmed address on an account that signs in with
  something else. The response is the same for every address. Links expire after 1 hour, work once, and stop if the
  address stops being eligible. Completing a reset from the link records the address as confirmed (the link
  could only be read in that inbox), and ends other sessions.
* **No recovery is invented** for phone-only or username-only accounts: the reset page tells them to add an email
  in My account while signed in, or to contact support.
* Email sign-ups left PENDING by PRD-01 (local data only; PRD-01 was never deployed) still activate through their
  link; the "Check your email" page remains but nothing redirects to it.

## Migrations

None. `email_ownership_required` (accounts 0006) is reused: new sign-ups set it, so an unconfirmed address never
signs in or recovers an account that has another identifier.

## Risks to accept or decide

1. A mistyped sign-up address belongs to someone else: that person can reset the password and take the account.
   The account cannot hold a paid membership, a wallet or an advertiser team until the address is confirmed, so it
   holds only the member's name. Owners who want this closed must bring back confirmation before the first reset
   (PRD-01) or ask for the current password as well.
2. Anyone can create an account with someone else's address, number or username (squatting). The real owner of an
   address can recover it by password reset; a number or username needs support.
3. The sign-up form now says when an email address or username is taken (previously only numbers). Attempts are
   rate-limited per IP and per identifier.
4. Unconfirmed email accounts receive no notification emails (inbox only) until they confirm.
