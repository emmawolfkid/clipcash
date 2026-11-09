# RG-03: Pre-deployment security hardening

Base commit: `5695093` (RG-02). All changes are uncommitted, awaiting project-owner review.
Scope: the security corrections required before restricted online deployment, from RG-01 (S1–S3) and RG-02.
Nothing was deployed. No migration ran against the development or any shared database.

## Corrections

| # | Finding | Severity | Correction |
|---|---|---|---|
| 1 | **Database guard functions could be redirected by a session's own temporary table.** The 38 trigger functions name tables without a schema and had no `search_path`, so names resolved through the caller's path, in which PostgreSQL searches `pg_temp` first. Reproduced before the fix: an **unbalanced journal entry committed**, and a posting reached a **frozen ledger account** | High (any login with TEMPORARY, which PUBLIC has by default; needs SQL access as the application's login) | Migration `core.0004_pin_function_search_path` sets `search_path = pg_catalog, public, pg_temp` on every function. Bodies, triggers and constraints are unchanged |
| 2 | Runtime database roles kept TEMPORARY (Render script granted it; PUBLIC has it by default) | Medium (defence in depth after #1) | `roles/01_create_roles.sql` grants CONNECT only and revokes CREATE and TEMPORARY from PUBLIC and both logins. The cPanel scripts already did. `roles/04_verify_runtime_role.sql` now probes temporary tables, schemas, unpinning a function and switching to the owner role, and checks every function is pinned |
| 3 | Forwarded client addresses were trusted from any peer (RG-01 S1). Behind Cloudflare on a host reachable directly, a forged `X-Forwarded-For` evaded per-address limits and falsified audit addresses; omitting it switched per-address limits off | High on NetPoa, none on Render | `DJANGO_TRUSTED_PROXY_NETWORKS`: forwarded information is believed only when the connecting peer is inside these networks; any other peer is the client. Optional `DJANGO_CLIENT_IP_HEADER` (`CF-Connecting-IP`, `X-Real-IP`, `True-Client-IP`) is read only from such peers. Outside Render, trusting proxies without a network list fails `check --deploy` (`accounts.E007`) unless `DJANGO_PROXY_ORIGIN_RESTRICTED` attests a web-server lock |
| 4 | Email-change links could be confirmed by anyone, without signing in (RG-01 S2) | Medium | The link now only moves its token into the session and redirects to a token-free review page. Completing the change needs the **signed-in owner** and a **recent password** (see policy). Another account is refused, audited, and does not consume the link |
| 5 | Other sessions survived an email change | Medium | The session hash covers the address as well as the password (`User._get_session_auth_hash`): every other session ends on its next request; the confirming session continues (`update_session_auth_hash`) |
| 6 | Email-change link paths were not redacted from application logs; token pages sent their URL as `Referer` to the site's own static requests (logged by Apache's combined format) | Low–Medium | `email-change/` added to `RedactTokenPaths`. Activation, email-confirmation, password-reset and email-change link responses send `Referrer-Policy: no-referrer` and `Cache-Control: no-store` |
| 7 | Unsafe settings passed the build (RG-01 S3): restricted demo off, console email backend, DEBUG, weak secrets, open hosts, non-HTTPS origins, HTTPS redirect off | Medium | New error-level deploy checks (below). Outside DEBUG the default email backend is now `dummy` (it was `console`, which prints account links) |

## Database migration `core.0004_pin_function_search_path`

* Forward-only (`reverse_sql=None`): un-pinning would reopen #1. Older code works with pinned functions, so a code
  rollback never needs it reversed.
* Depends on the last migration of every app that creates a function, so it runs after all of them.
* Needs the table **owner** (migration identity: `clipcash_migrator` on Render, the owner login on cPanel); the
  runtime login is refused `ALTER FUNCTION` (and `04_verify_runtime_role.sql` proves it).
* No table, row or privilege changes. Runs in milliseconds.
* A future migration that adds or replaces a function must include `SET search_path = pg_catalog, public, pg_temp`
  (`CREATE OR REPLACE` drops an earlier `SET`). `FunctionSearchPathTests` fails otherwise.

**Execution order on a deployed database:** back up → `migrate` as the owner/migrator → re-run
`roles/01_create_roles.sql` (Render, as the default user, to withdraw TEMPORARY) or `cpanel/01_prepare_database.sql`
(cPanel) → `roles/03_grant_runtime.sql` / `cpanel/02_grant_runtime.sql` → verify as the runtime login with
`roles/04_verify_runtime_role.sql` (and `cpanel/03_verify_separation.sql`). Nothing has been deployed yet, so the
first deployment simply includes it.

## Email-change behaviour

1. Request (unchanged): signed-in, verified account; current password; a link goes to the new address and a notice to
   the current one. The password entry also marks this session as recently re-verified.
2. Link (`/accounts/email-change/<id>/<token>/`, GET only): checks the token, stores it in the session, redirects to
   `/accounts/email-change/<id>/confirm/`. Changes nothing. POST is refused (405).
3. Review (`…/confirm/`): requires sign-in (a signed-out reader is sent to sign in and returns, the session keeping
   the token), the link opened in this browser, and the account that asked. Another account gets the invalid-link
   page; the attempt is audited (`account.email_change.denied`, `other_account`) and the owner's link still works.
4. **Freshness policy** (`ACCOUNTS_REAUTHENTICATION_SECONDS = 300`, `apps/accounts/reauth.py`): if this session
   confirmed the current password of this account within 5 minutes (on the request or on this page), the page shows
   a button only; otherwise it asks for the password. Signing in alone does not count. Wrong passwords use the existing
   per-account limit (5 per 15 minutes) and are audited (`auth.current_password.failed`, operation
   `email_change_confirm`).
5. On success: the address changes and stays verified, the request row is deleted (single use), the previous address
   is notified, the audit event and in-app notice are written as before, every other session ends and this one
   continues.

Unchanged: tokens expire after 24 hours dated to the request, are single-use, are bound to the account's address,
password and status, and are never stored in the outbox or audit records. Restricted-demo waiver accounts still cannot
change their address, and changes stay closed while the `dummy` email backend is in use.

Consequence: an address changed by staff in the admin now also ends that account's sessions (it then confirms the
new address after signing in, as before).

## Client addresses

| Setting | Render | NetPoa (Cloudflare → Passenger) |
|---|---|---|
| `DJANGO_TRUSTED_PROXY_COUNT` | `2` (unchanged) | `1` |
| `DJANGO_TRUSTED_PROXY_NETWORKS` | unset (internal load balancer) | Cloudflare's published ranges |
| `DJANGO_CLIENT_IP_HEADER` | unset | unset, or `CF-Connecting-IP` with the count at `0` |
| `DJANGO_PROXY_ORIGIN_RESTRICTED` | unset | unset (only with a verified `.htaccess` lock and no network list) |

If the web server already rewrites the peer address (mod_remoteip / LiteSpeed), use count `0` and no network list;
the server's own trust list must then contain only Cloudflare. `/manage/health/` now also shows whether the
connection came from a trusted network.

## Deployment checks (`check --deploy --fail-level ERROR`)

New errors: `core.E002` DEBUG on · `core.E003` missing, short, placeholder or `django-insecure` secret ·
`core.E004` empty or wildcard allowed hosts · `core.E005` non-HTTPS or over-broad CSRF origins · `core.E006` HTTPS
redirect off · `core.E007` restricted demo off without `DJANGO_PUBLIC_REGISTRATION_APPROVED` · `core.E008` any
real-money feature enabled · `notifications.E001` console, locmem or file email backend · `notifications.E002`
TLS and SSL both on · `notifications.E003` remote SMTP without TLS/SSL · `accounts.E005` client-address header without
networks · `accounts.E006` invalid network · `accounts.E007` proxies trusted from any peer outside Render ·
`accounts.E008` unsupported header · `videos.E003`/`E004` malformed Bunny library id or CDN host.
New warning: `accounts.W003` networks without a proxy count or header.
Existing checks are unchanged (`payments.E001` live payments, `payments.E002` sandbox, `videos.E002` demo provider,
`accounts.E001`–`E004`). Missing Bunny settings stay a warning (`videos.W001`): the provider refuses every playback
until all four are set, so nothing is exposed.

These checks read configuration only. They cannot see the hosting facts (who can reach the origin, what the web
server logs, the database roles); the runbooks verify those on the host.

## Database privilege requirements

Runtime login: CONNECT on the database, USAGE on schema `public`, data privileges from `03`/`02_grant_runtime.sql`;
**no** CREATE or TEMPORARY on the database, no CREATE on `public`, no ownership, not a member of the owner, not
superuser/CREATEROLE/CREATEDB/REPLICATION/BYPASSRLS. Owner/migration login: owns every table and function; used only for
migrations, `createcachetable`, the grant scripts, backups and `audit_purge`.

## Token logging: what remains

Application code cannot stop the web server, Cloudflare or a browser from recording the URL of the **first**
request to an emailed link. After that request, activation, password-reset and email-change tokens live only in the
session; email-confirmation links still POST to their own URL (single-use, 24 h). Mitigations: no Referer, no
caching, redacted application logs, short lifetimes, single use, and (email change) the signed-in owner plus password.
On NetPoa, keep raw access logs restricted to the account owner and disable log archiving if not needed.

## Rollback

* Code: redeploy the previous release. Its views, checks and settings work with the pinned functions.
* Migration: do not reverse `core.0004` (it has no reverse); it is harmless to older code.
* Sessions: deploying this release signs everyone out once (the session hash changed).
* Settings: the new variables are optional on Render; removing `DJANGO_TRUSTED_PROXY_NETWORKS` on NetPoa makes the
  build fail by design (`accounts.E007`).
* Role scripts: re-running `01` is idempotent; `02` is still never repeated.

## Verification

See the RG-03 report for the exact commands and results: focused suites, the full PostgreSQL suite, the forward
migration on the disposable test database, and the verification scripts run against that database.

**Not verified locally:** a genuine owner/runtime role split. The local login has neither CREATEROLE nor CREATEDB, so
separate roles and a fresh database could not be created here; the verification scripts were run as the single owner
login, where they correctly **fail**. The first proof with separated roles is the NetPoa checklist §3.3 (or Render
`04_verify_runtime_role.sql`).
