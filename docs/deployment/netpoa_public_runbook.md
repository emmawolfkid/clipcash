# Public-production deployment runbook (NetPoa cPanel, `.co.tz`)

For the person who deploys ClipCash publicly on NetPoa (cPanel, Passenger, PostgreSQL), with open registration
and, **only after separate written authorization**, manual Airtel Lipa membership payments. The restricted-demo
runbook ([netpoa_runbook.md](netpoa_runbook.md)) stays valid for demo deployments and is not replaced; where a
step is identical, this runbook points to its section instead of repeating it.

**Not rehearsed.** No part of this runbook has been run on NetPoa. Do the first run on a staging subdomain
(e.g. `staging.<domain>.co.tz`), then repeat for the real host.

## Ground rules

* **Do not start** until [netpoa_public_compatibility.md](netpoa_public_compatibility.md) Part 2 is complete: every
  row PASS or a written owner decision. In particular PostgreSQL 15+, `btree_gist`, owner/runtime separation, cron
  and TLS SMTP.
* **Real membership payments stay OFF** through Stage 1. `DJANGO_MANUAL_MEMBERSHIP_PAYMENTS` is not set (default
  off). Stage 2 (§17) needs the owner's written authorization, the real Airtel Lipa business details from the owner,
  and the open payment-policy decisions (refunds, verifiers) settled. Without them: no payment collection.
* **Locked in every stage:** live payments, real-money rewards, withdrawals, advertiser settlement and sandbox
  payments. Nothing here unlocks them.
* **Secrets:** never in a report, chat, commit, crontab line, `.htaccess`, Setup Python App variable or log. Type
  them with `read -rs` / `psql -W`. The database **owner** credential is never stored on the server.
* Keep a private, offline record: release hash, dates, who did each step, non-secret settings.

## 0. Inputs to collect first

| Item | From | Notes |
|---|---|---|
| Release | Reviewed commit on `main` | Write down its hash; it must pass the full PostgreSQL suite |
| Domain | Owner | `<domain>.co.tz` registered; DNS access (registrar or Cloudflare) |
| Hosting evidence | [netpoa_public_compatibility.md](netpoa_public_compatibility.md) | All PASS / decided |
| SMTP | Email provider or NetPoa | Host, port 587/465, TLS, user, password, verified sender on the `.co.tz` domain, SPF/DKIM |
| Bunny Stream | Bunny account | Library ID, API key, CDN host, token key; **Embed view token authentication on** |
| People | Owner | Two Super Administrators (different people); named payment verifiers for Stage 2 |
| Plans | Owner | Names, prices (TZS), lengths (days) — entered in the console, never in code |
| Airtel Lipa | Owner (Stage 2 only) | Business number and recipient name exactly as Airtel issued them |
| Your PC | — | PostgreSQL 18 client tools, Git |

## 1. Exact order of operations

| # | Step | Section |
|---|---|---|
| 1 | Domain and DNS | §2 |
| 2 | Release upload | §3 |
| 3 | Python environment (Setup Python App, virtualenv, dependencies) | §4 |
| 4 | PostgreSQL provisioning (database, runtime user, privileges closed) | §5 |
| 5 | Environment variables (`.env`) and `check --deploy` | §6 |
| 6 | Database backup (before any migration on an existing database) | §7 |
| 7 | Database migrations (owner login) | §8 |
| 8 | Runtime privilege grants and verification | §9 |
| 9 | Static file collection | §10 |
| 10 | Passenger application start and HTTPS | §11 |
| 11 | Cron (jobs and health) | §12 |
| 12 | SMTP verification | §13 |
| 13 | Bunny Stream verification | §14 |
| 14 | Administrators, site configuration, plans | §15 |
| 15 | Registration acceptance | §16 |
| 16 | Production health checks | §18 |
| 17 | First backup and restore drill | §19 |
| — | **Stage 2 only, after written authorization:** manual payment enablement and acceptance | §17 |

## 2. Domain and DNS

1. Decide the host: the apex `<domain>.co.tz` and/or `www.<domain>.co.tz` (pick one canonical host; list both in
   allowed hosts only if both serve the app). Start with a staging subdomain.
2. cPanel → Domains: add the domain or subdomain, with a document root **outside** the application folder (cPanel
   writes only Passenger's `.htaccess` there).
3. DNS (registrar or Cloudflare): `A`/`AAAA` (or `CNAME`) to NetPoa's server. With Cloudflare: proxied, SSL mode
   **Full (strict)** (never "Flexible"), "Always Use HTTPS" on. Without Cloudflare: AutoSSL in cPanel.
4. Email DNS for the sender domain: SPF, DKIM (and DMARC) as the SMTP provider instructs. Without them activation
   emails are likely to be filtered as spam.
5. Wait for propagation; `dig +short <host>` shows the expected addresses.

## 3. Release

As [netpoa_runbook.md §1 and §3](netpoa_runbook.md): `git archive` of the reviewed commit on your PC, upload to
`~/clipcash/releases/`, unpack, `rsync` into `~/clipcash/app/` (excluding `.env`, `staticfiles/`, `tmp/`, logs and
`media/*`), folders mode 700. Code, `.env`, logs and backups never go in a web-served folder.

## 4. Python environment

As [netpoa_runbook.md §4](netpoa_runbook.md): Setup Python App → Python **3.13** (or the version approved in the
checklist), application root `clipcash/app`, URL = the ClipCash host, startup file `passenger_wsgi.py`, entry point
`application`. **Do not enter environment variables on that page.** Then in the virtualenv:
`pip install -r requirements/base.txt` and note `which python` for cron.

## 5. PostgreSQL provisioning

As [netpoa_runbook.md §5 steps 1–2](netpoa_runbook.md): create database and the separate runtime user in cPanel,
then close database-level privileges as the owner login with `scripts/database/cpanel/01_prepare_database.sql`.
Any `STOP:` line → stop; never fall back to one all-powerful login.

## 6. Environment variables (`~/clipcash/app/.env`, mode 600)

Create as in [netpoa_runbook.md §6](netpoa_runbook.md) (umask 077, new secret key). **Public-production values**:

| Variable | Stage 1 value | Note |
|---|---|---|
| `DJANGO_SECRET_KEY` | Newly generated | Never reused |
| `DJANGO_DEBUG` | `False` | |
| `DJANGO_ALLOWED_HOSTS` | the `.co.tz` host(s) | Exact names |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://<host>` for each | |
| `DATABASE_URL` | runtime login URL | Never the owner login |
| `DJANGO_CACHE_URL` | `dbcache://clipcash_cache` | `createcachetable` in §8 |
| `DJANGO_SECURE_SSL_REDIRECT` | `True` | |
| `DJANGO_SECURE_PROXY_SSL_HEADER` | `False` with Cloudflare Full (strict) to Passenger | Per checklist C1 |
| `DJANGO_SECURE_HSTS_SECONDS` | `0` | Raise only after a week of working HTTPS, in a reviewed change |
| `DJANGO_TRUSTED_PROXY_COUNT` / `DJANGO_TRUSTED_PROXY_NETWORKS` / `DJANGO_CLIENT_IP_HEADER` | Per checklist C1, as in [netpoa_runbook.md §6 and §9](netpoa_runbook.md) | Verified in §18 |
| `DJANGO_RESTRICTED_DEMO` | **`False`** | Public launch |
| `DJANGO_PUBLIC_REGISTRATION_APPROVED` | **`True`** | Only with the owner's launch approval; otherwise `core.E007` |
| `DJANGO_PAYMENTS_MODE` | `disabled` | Sandbox never in production |
| `DJANGO_MANUAL_MEMBERSHIP_PAYMENTS` | **unset** (off) | Stage 2 only (§17) |
| `VIDEO_PROVIDER` | `bunny` | |
| `BUNNY_STREAM_LIBRARY_ID`, `BUNNY_STREAM_API_KEY`, `BUNNY_STREAM_CDN_HOSTNAME`, `BUNNY_STREAM_TOKEN_SECURITY_KEY` | From Bunny | |
| `DJANGO_EMAIL_BACKEND` | `django.core.mail.backends.smtp.EmailBackend` | **Required** for public registration |
| `DJANGO_EMAIL_HOST`, `DJANGO_EMAIL_PORT`, `DJANGO_EMAIL_USE_TLS` or `DJANGO_EMAIL_USE_SSL`, `DJANGO_EMAIL_HOST_USER`, `DJANGO_EMAIL_HOST_PASSWORD` | From the provider | TLS required (`notifications.E003`) |
| `DJANGO_DEFAULT_FROM_EMAIL` | `ClipCash <no-reply@<domain>.co.tz>` | Verified sender |
| `DJANGO_MEMBERSHIPS_MAX_PLAN_PRICE` | unset (TZS 100,000,000) or an owner-approved ceiling | |
| `DJANGO_JOBS_HEARTBEAT_STALE_SECONDS` | `900` only if cron is every 5 minutes | |

**Never set in public production:** `DJANGO_DEBUG=True`; `DJANGO_RESTRICTED_DEMO=True` together with
`DJANGO_MANUAL_MEMBERSHIP_PAYMENTS` (`payments.E003`); `DJANGO_PAYMENTS_MODE=sandbox` or
`DJANGO_PAYMENTS_SANDBOX_ALLOWED` (`payments.E002/E004`); `DJANGO_MEMBERSHIPS_COMPLIMENTARY_GRANTS`;
`DJANGO_DEMO_EMAIL_VERIFICATION_OPTIONAL`; `DJANGO_DEMO_ACCOUNTS`; `VIDEO_DEMO_ALLOWED`; a dummy, console,
locmem or file email backend; `DJANGO_ADVERTISING_REPORT_MIN_COUNT` below 5; the owner database login.

Then: `python manage.py check --deploy --fail-level ERROR`. Expected: no errors; only `security.W004` (HSTS 0),
plus `videos.W001` until the Bunny values are filled in. Any error → fix the value before continuing. (These
Stage 1 and Stage 2 profiles were run through this command in NET-01 with placeholder hosts: both pass; Stage 2
combined with restricted demo fails `payments.E003`; public registration without approval fails `core.E007`.)

## 7. Database backup

* **New, empty database:** nothing to back up yet; continue.
* **Existing database (any later deploy with migrations):** back up first, as the owner login, with
  `scripts/database/backup.sh` ([netpoa_runbook.md §12](netpoa_runbook.md)). Download the `.dump` and `.sha256`,
  store them encrypted, delete the server copy. Do not migrate without a verified backup.

## 8. Database migrations (owner login)

As [netpoa_runbook.md §5 step 3](netpoa_runbook.md): with the owner URL read by `read -rs` into `DATABASE_URL` for
this shell only:

```bash
python manage.py migrate --plan          # review: expect accounts.0003, core.0004, payments.0004, payments.0005 on a demo-era database
python manage.py migrate --noinput
python manage.py createcachetable
unset DATABASE_URL
```

The owner login must run them: `core.0004` alters functions and `payments.0005` creates trigger functions.
**Rollback is restore, not reverse:** reversing `payments.0004` would drop the payment tables and their records.

## 9. Runtime privilege grants and verification

As [netpoa_runbook.md §5 steps 4–5](netpoa_runbook.md): `02_grant_runtime.sql` as the owner, then
`03_verify_separation.sql` and `roles/04_verify_runtime_role.sql` **as the runtime login**; both must end with
"passed". The grant script revokes DELETE wherever a delete-guard trigger exists, which now includes the manual
payment tables. Re-run steps 5 and 9 after any change on cPanel's PostgreSQL Databases page.

## 10. Static files

```bash
python manage.py collectstatic --noinput
```

WhiteNoise serves the hashed files through Passenger; no web-served static folder.

## 11. Passenger start and HTTPS

1. `mkdir -p tmp && touch tmp/restart.txt`; open `https://<host>/healthz` → `ok`.
2. HTTPS and the proxy boundary exactly as [netpoa_runbook.md §9](netpoa_runbook.md) (AutoSSL or Origin CA,
   Cloudflare Full (strict), client-address case from checklist C1, optional origin lock to Cloudflare).

## 12. Cron

Exactly the two lines of [netpoa_runbook.md §11](netpoa_runbook.md) (`run_jobs_cron.sh` every minute,
`jobs_health_cron.sh` every 10 minutes), Cron Email set to an address someone reads. In public production the
jobs deliver **activation, password-reset and payment emails** and expire memberships; without cron nobody can
finish registering. After five minutes: `python manage.py jobs_status` exits 0 and `/manage/health/` shows jobs
fresh.

## 13. SMTP verification

```bash
python manage.py email_diagnostics --connect
python manage.py email_diagnostics --send-to <an address you read>
```

The test message must arrive within about a minute (cron delivers it), not in spam. Check SPF/DKIM pass in the
received headers. **Do not open registration publicly until this passes.**

## 14. Bunny Stream verification

As [netpoa_runbook.md §8](netpoa_runbook.md): upload and publish one video you own; it plays for a member with an
active membership; its embed URL **without** a token is refused in a private window. If it plays without a token,
turn on Embed view token authentication before launch.

## 15. Administrators, site configuration and plans

1. `python manage.py createsuperuser` for two different people (strong unique passwords; no shared accounts).
2. `/admin/` → Site configuration: time zone `Africa/Dar_es_Salaam`, support email/phone, memberships on, videos on;
   public registration on; advertisers only if approved.
3. `/manage/plans/` (Super Administrator): create the owner-approved plans (name, TZS price, length in days,
   description, listed). Nothing is pre-filled; prices and lengths can be changed later without a deploy, and
   existing choices keep their quoted terms.
4. Staff permissions in `/admin/`, one named person each, recorded privately. **Do not grant
   `payments.verify_manual_payments` until Stage 2.**

## 16. Registration acceptance (public)

With a real mailbox you control:

| Check | Expected |
|---|---|
| `/accounts/register/` | 200, form shown |
| Register an address | Activation email arrives (cron + SMTP); link sets a password; account active and verified |
| Register the same address again | Same response; an "already registered" email; no second account |
| Sign in / sign out / password reset email | All work |
| New member: `/memberships/mine/`, a members-only video | No membership; playback refused |
| Plans page | The configured price and length; notice that payment is not yet available (Stage 1) |
| Choose a plan | Saved as pending; **no payment page** (manual payments off); nothing activates |
| `/admin/` → Site configuration → public registration off | `/accounts/register/` answers 403 (rollback lever) |

## 17. Stage 2 — manual Airtel Lipa payments (only after written authorization)

**Preconditions, all required:** the owner's written authorization for real payment collection; refund/reversal
procedure and verifier staffing decided ([PAY-01 doc §6 and §12](../implementation/pay_01_manual_membership_payments.md));
the real Lipa business number and recipient name from the owner; Stage 1 accepted; a fresh backup.

1. `.env`: add `DJANGO_MANUAL_MEMBERSHIP_PAYMENTS=True` (keep `DJANGO_PAYMENTS_MODE=disabled`,
   `DJANGO_RESTRICTED_DEMO=False`); `check --deploy --fail-level ERROR` passes with no `payments.E00x`;
   `touch tmp/restart.txt`.
2. Before details are published, members see "Payments are temporarily unavailable" — nothing can be submitted.
3. `/manage/membership-payments/instructions/` (Super Administrator): publish the business number and recipient name
   **exactly** as Airtel issued them; a second person checks them against the Airtel paperwork.
4. Grant `payments.verify_manual_payments` to the named verifiers (at least two people recommended).
5. **Acceptance with one small real payment** by an owner-controlled member account, on a cheap test plan created
   for this purpose (deactivate it afterwards): choose → pay the exact amount from the owner's phone → submit the
   reference → confirm the membership is **not** active → a verifier finds the transaction in the official Airtel
   business portal/statement → approve with the re-typed amount and reference → membership active for the plan's
   length → payment and activation notifications received → `/manage/membership-payments/` shows it approved.
   Also test a deliberately wrong reference: reject it and see the member notice.
6. Reconcile that day: Airtel statement total = approved claims = `membership.manual_payment_received` ledger
   postings (CASH). Refund the test payment (if wanted) by the manual procedure, cancel the test membership with
   reason Refund, and have a Super Administrator reverse its ledger entry.
7. **Off switch:** retire the payment details in the console (stops new claims, keeps pending ones reviewable) or
   remove `DJANGO_MANUAL_MEMBERSHIP_PAYMENTS` from `.env` and restart (pages 404, nothing is lost).

## 18. Production health checks

| Check | Expected |
|---|---|
| `curl -I http://<host>/` | Redirect to `https://` |
| `curl https://<host>/healthz` | `ok` (also checks the database) |
| Headers on `/accounts/login/` | `Secure` cookies, `Content-Security-Policy`, `X-Frame-Options: DENY` |
| `/manage/health/` (Super Administrator) | Privilege separation Healthy; background jobs fresh; email configured; client address = your real IP (also with a forged `X-Forwarded-For`) |
| Direct-origin request with a forged `X-Forwarded-For` | As [netpoa_runbook.md §13](netpoa_runbook.md): the forged address never appears |
| Financial locks | Wallet and withdrawals pages show no live money; no sandbox payment pages (404) |
| `python manage.py check --deploy --fail-level ERROR` | Passes |
| External uptime monitor | On `https://<host>/healthz` |

## 19. First backup and restore drill

As [netpoa_runbook.md §12](netpoa_runbook.md): backup as the owner, download, restore drill on your PC with
`restore_check.sh` (expect `13 of 13` lock triggers and `2 of 2` lock constraints). That script predates PAY-01 and
does not yet list the manual-payment guards, so also run on the drill database:

```sql
SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgenabled = 'O'
  AND tgname IN ('payments_manual_claim_guard', 'payments_manual_claim_no_delete',
                 'payments_manual_instruction_guard', 'payments_manual_instruction_no_delete');
```

Expect `4`. Then compare row counts (accounts, audit events, journal entries, manual payments) with the source.
Daily backups from launch; before every deploy with migrations.

## 20. Later deploys and rollback

Deploys: [netpoa_runbook.md §14](netpoa_runbook.md) (backup → release → `pip install` → migrations as owner →
grants/verification → `check --deploy` → `collectstatic` → restart → §18). Rollback:
[netpoa_runbook.md §15](netpoa_runbook.md), plus these public-production levers:

| Situation | Action |
|---|---|
| Registration problem (spam, email failing) | Site configuration → public registration off (immediate; no restart) |
| Payment problem | Retire the Lipa details in the console, or remove `DJANGO_MANUAL_MEMBERSHIP_PAYMENTS` and restart |
| Wrong plan price | Edit or deactivate the plan in `/manage/plans/`; existing choices keep their quotes; affected payments are handled by the refund procedure |
| Serious incident | Maintenance mode in Site configuration, or Stop the app in Setup Python App; remove cron lines |
| Bad migration or data | Restore the last verified dump into a new database (owner, `--no-owner --no-privileges`), re-run §5, §9, point `DATABASE_URL` at it. Never reverse migrations |

## 21. Monitoring

As [netpoa_runbook.md §16](netpoa_runbook.md), plus: the verification queue count on `/manage/` (payments awaiting
verification, checked at least daily in Stage 2), the daily Airtel reconciliation, and the email bounce/complaint
reports from the SMTP provider.

## 22. Known risks

All of [netpoa_runbook.md "Known risks"](netpoa_runbook.md) (access logs record each emailed link once, shared
resource limits, owner credential, no staff MFA), plus:

* **Public exposure:** registration is open to anyone; rate limits (per IP and per address) depend on the
  client-address configuration being right (§18).
* **Email dependency:** if SMTP or cron stops, nobody can finish registering or reset a password.
* **Manual payments:** verification depends on staff checking the official Airtel records; squatted references and
  mismatched amounts need the documented handling. Refunds are manual.
* **No external alerting** beyond cron email and an optional uptime/heartbeat service.
