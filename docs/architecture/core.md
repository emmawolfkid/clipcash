# Core system foundation (Phase 02A)

Status: implemented in `apps/core` (models, services, flags, money) and
`apps/administration` (admin UI). The financial ledger, audit-event storage and
background workers are **not** part of this phase.

## Configuration ownership

| Owner | Settings | Changed by | Where |
| --- | --- | --- | --- |
| Environment | `DJANGO_SECRET_KEY`, database credentials, `DEBUG`, allowed hosts, CSRF origins, HTTPS/HSTS, cookies, cache, email backend, trusted proxies, log level | Operators, via `.env` / deployment environment | `config/settings.py` |
| Code | Supported languages (English, Swahili), supported currencies (TZS), financial feature locks, token lifetimes, rate limits | Developers, via reviewed commits | `config/settings.py`, `apps/core/money.py`, `apps/core/features.py`, `apps/accounts/throttling.py` |
| Database | Platform name and description, company and support contacts, website, default time zone / currency / language, maintenance mode and message, business feature flags | ACTIVE Super Administrators only | `core.SiteConfiguration` (Django admin → Site configuration) |

There is no web path to view or change environment- or code-owned settings.

### SiteConfiguration

* Exactly one row (`id = 1`), created by migration `core.0002`; a database
  check constraint rejects any other id. No add or delete in the admin; the
  migration's reverse step never deletes configuration.
* Validated server-side: https-only website, E.164 support phone (normalized
  from local formats such as `0712 345 678`), IANA time zone, supported
  language, TZS currency (also a database constraint), non-blank name.
* Optimistic concurrency: every successful change increments `version`. The
  editor submits the version it loaded; `update_site_configuration()` locks the
  row and rejects stale versions, so concurrent editors cannot silently
  overwrite each other.
* Reads use `apps.core.services.get_site_configuration()`: cached for 60 s,
  invalidated immediately and again after commit. If the row is missing or the
  database is unavailable it returns an unsaved instance with the defaults and
  logs an error; reads never write.

### Per-request behaviour (`SiteConfigurationMiddleware`)

* Activates the configured time zone (storage stays UTC).
* Applies the default language only when the visitor has no language cookie and
  the browser sends no `Accept-Language` header.
* Maintenance mode: returns `503` with `Retry-After` to everyone except signed-in
  ACTIVE staff. Sign-in, sign-out, the admin and language switching stay
  reachable so staff can get in.

## Feature flags (`apps.core.features`)

| Flag | Default | Control |
| --- | --- | --- |
| `registration` | on | Site configuration |
| `memberships` | off | Site configuration |
| `videos` | off | Site configuration |
| `advertisers` | off | Site configuration |
| `real_money_rewards` | off | **Locked in code** |
| `live_payments` | off | **Locked in code** |
| `withdrawals` | off | **Locked in code** |

* `is_enabled(key)` returns the stored value for business flags, the explicit
  default if the stored value is missing or not a boolean, and always `False`
  for financial flags.
* Financial flags have no database field, environment variable or admin
  control. Enabling them requires a later, reviewed code change after legal,
  payment-provider and operational sign-off. Membership *availability* can be
  switched on, but taking membership fees depends on `live_payments`, which
  stays locked.
* Unknown flag names raise `UnknownFeatureError` (a programming error, caught by
  tests), including at import time for `@feature_required("...")`.
* `@feature_required(key)` returns 404 when a flag is off. Registration shows a
  "registration closed" page instead; already-registered PENDING accounts can
  still activate.

## Shared model conventions (`apps.core.models`)

* `UUIDModel`: random UUID primary key (not guessable in URLs).
* `TimeStampedModel`: `created_at`, `updated_at`.
* `BaseModel`: both; the default for new ClipCash models.

The existing `accounts.User` model keeps its own equivalent fields and is not
migrated onto these bases.

## Money conventions (`apps.core.money`)

* Amounts are **integers in whole Tanzanian shillings**. ISO 4217 defines two
  decimal places for TZS, but senti are not used in practice and mobile-money
  providers settle in whole shillings. Fractions are rejected, never rounded.
* Floats and booleans are rejected everywhere; `Decimal` is accepted only when
  integral. Any single amount is limited to ±10^15.
* `Money(amount, currency="TZS")` is immutable. Addition, subtraction and
  comparison require the same currency; multiplication only by integers.
* `Money.allocate(ratios)` splits with the largest-remainder method, so parts
  always sum to the original amount.
* `parse_amount()` accepts `1500`, `1,500`, `TZS 1,500`; rejects decimals,
  misplaced separators and other currencies. `format_amount()` and the template
  filter `{{ amount|tzs }}` render `TZS 1,500`.
* `AmountField` (a `BigIntegerField`) adds amount validators; models must also
  add database check constraints for sign rules.

No balances, wallets or ledger postings exist yet (Phase 02B and later).

## Administration permissions

| Action | MEMBER | CONTENT_MANAGER (even with all `core` permissions) | SUPER_ADMIN |
| --- | :---: | :---: | :---: |
| View site configuration | ✘ | ✘ | ✔ |
| Change site configuration | ✘ | ✘ | ✔ (validated, versioned) |
| Add or delete the configuration row | ✘ | ✘ | ✘ |
| Change financial flags | ✘ | ✘ | ✘ (locked in code) |

Checks run in the service as well as the admin, so a direct call from code is
held to the same rules. `SuperAdminOnlyMixin` in `apps.administration.admin` is
reusable for future sensitive admin screens.

## Future audit integration

Configuration changes are currently written to the `clipcash.security` log as
`site_configuration_changed actor=<id> version=<n> fields=<names>` (field names
only, no values). When the audit app lands (Phase 02B), the same call site in
`update_site_configuration()` will also write a permanent audit event holding
the actor, version and before/after values inside the same transaction.
