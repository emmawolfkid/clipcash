# REG-03 flexible member registration and FAV-01 favicon repair

Local and uncommitted on `port/mariadb-schema`, on top of the deployed baseline `5e8cc2a`. The main worktree
and its CR-01 changes are not edited. Test records are fictional.

## Registration and sign-in

* Sign-up asks for a first name, a last name, ONE sign-in identifier and a password. The member chooses a
  Tanzanian mobile number, an email address or a username; only the chosen one is required and used (the
  other two boxes are ignored, and hidden when JavaScript runs).
* Usernames (`apps/accounts/usernames.py`): 3 to 30 characters, ASCII letters, digits, dots and underscores,
  starting with a letter (so never readable as a phone number), never containing `@` (so never readable as an
  email), no `..`, no trailing dot, a few staff-like names reserved. Stored lowercased; unique, and unique
  ignoring case (database index plus a case-insensitive unique backstop and a format check).
* The sign-in box accepts any of the three. `@` means email, no letters means phone (normalized to E.164 as
  before), anything else is a username (any letter case). Existing members and staff sign in exactly as before;
  roles, groups, permissions and staff verification are unchanged. Login rate limits key on the normalized
  identifier, so a username in another letter case is the same account.
* Mobile and username accounts are usable at once. An email account is usable once its address is confirmed:
  the confirmation link is queued at sign-up. Email stays optional for mobile and username accounts. A mobile
  number stays unverified (`phone_verified=False`) until ownership is confirmed.
* Only a confirmed email address can receive a password-reset link. A username or phone number never resets a
  password; the account page and the reset page say so.
* Username-only accounts reach the dashboard, choose a plan and reach checkout (`identity_ready`). Every current
  payment method is mobile money, so checkout asks for the number paid from; it is pre-filled only when the
  account has a number. No payment rule changed.
* Passwords (`apps/accounts/password_validation.py`), applied whenever a password is set: registration, password
  change, reset, activation and the admin. Members: at least 8 characters with at least one letter AND at least
  one number; spaces are allowed, so a short phrase with a number works. Staff (and an account not yet known):
  unchanged, at least 10 characters with no letter/number rule. Everyone: Django's checks against personal
  information (name, username, email, mobile number), common passwords and all-numeric passwords. Sign-in never
  re-checks a stored password, so existing passwords keep working. The guidance under each new-password field
  describes the policy of that account. Every password field on the account pages has a Show password button
  (`aria-pressed`, `aria-controls`); Django's own admin login has none.
* Duplicate identifiers are reported on sign-up (unavoidable for instant sign-up, as before for numbers) and
  are rate-limited per IP and per identifier.

## Favicon

Pages extending `base.html` linked only `favicon.svg`; it loaded, but there was no raster icon and nothing at
`/favicon.ico` (404), which browsers and other clients that ignore SVG icons request (older Safari, iOS home
screen, link previews), and Django admin pages linked no icon at all. Now:

* `scripts/generate_favicons.py` draws `static/images/favicon.ico` (16, 32, 48 px) and `apple-touch-icon.png`
  (180 px) from the SVG's geometry, deterministically (a test keeps the files equal to the generator's output);
* `templates/components/favicon_links.html` links all three, from `base.html` and from the admin
  (`templates/admin/base_site.html`, Django's own template plus the links);
* `/favicon.ico` redirects (302, cached one day) to the collected, content-hashed ICO.

Checked in real Google Chrome 157 (headless, Playwright) with `DEBUG=False`, `collectstatic` and WhiteNoise's
manifest storage, on the public home, sign-in, registration and admin login pages: each links the three icons,
each returns 200 with its type and Chrome decodes it (ICO 48x48 largest image, SVG, PNG 180x180);
`/favicon.ico` redirects to the ICO; Show password toggles by click and by keyboard on the sign-in and
registration fields; no console errors (45 of 45 checks). Test on NetPoa after deploying (the hashed names
follow from the file contents, so they stay the same unless an icon changes):

* `https://clipcash.co.tz/favicon.ico` → 302 → `https://clipcash.co.tz/static/images/favicon.41dd0204d29c.ico`
  (200, `image/x-icon`, 1408 bytes)
* `https://clipcash.co.tz/static/images/favicon.80dbb46961ad.svg` (200, `image/svg+xml`)
* `https://clipcash.co.tz/static/images/apple-touch-icon.13b0bfb7f1cc.png` (200, `image/png`)

## Migration

`accounts.0005_username_sign_in` adds the nullable `username` column and its constraints, and widens the
"has a sign-in identifier" check to email or phone or username. No existing row changes.
`scripts/database/check_username_upgrade.py` rehearses it on a fresh `test_*` MariaDB database with existing
staff and member accounts (18 checks) and drops that database.
