# Authentication and account security (Phase 01)

Status: implemented in `apps/accounts`. Real-money features (payments, rewards,
withdrawals) are **not** part of this phase and remain disabled.

## Components

| Module | Responsibility |
| --- | --- |
| `models.py` / `managers.py` | Custom `User` (UUID pk, email login, optional E.164 phone, role, status). Normalization, derived access flags, database constraints, last-Super-Administrator guard, email change clears verification, no return to PENDING. |
| `backends.py` | `AccountStatusBackend`: only ACTIVE accounts authenticate, at login and on every request. |
| `middleware.py` | `AccountStatusMiddleware`: flushes sessions of suspended/disabled accounts and shows the "account unavailable" page. |
| `services.py` | Registration, activation, email confirmation, centralized role/status changes (`change_access`, `assign_role`, `change_status`). |
| `tokens.py` | Single-use, expiring verification and reset tokens bound to account state. |
| `throttling.py` / `checks.py` | Cache-backed rate limits, trusted-proxy client IP, startup and deploy checks. |
| `access.py` | `verified_account_required`, `role_required`, `VerifiedAccountRequiredMixin`, `RoleRequiredMixin`. |
| `roles.py` | Role-to-Group mapping and group synchronization. |
| `admin.py` | Hardened Django admin. |
| `config/test_runner.py` | Refuses non-PostgreSQL or non-`test_*` test databases. |

## User lifecycle

```
          register (email only)
                 │
                 ▼
             PENDING ── unusable password, cannot sign in
                 │  owner opens emailed link (24 h, single use)
                 │  and chooses a password
                 ▼
             ACTIVE ◄──────────► SUSPENDED
                 │                   │
                 └──────► DISABLED ◄─┘      (PENDING ──► DISABLED also allowed)
                            │
                            └──► ACTIVE   (Super Administrators only)
```

* Nothing returns to PENDING. PENDING → ACTIVE happens only through activation.
* Only ACTIVE accounts can sign in. Suspending or disabling an account ends its
  sessions on the next request.
* Changing an email address clears `email_verified`; ACTIVE accounts then
  confirm the new address with a single-use link before using member pages.
* Member pages require ACTIVE **and** a verified email.

### Why registration takes no password

If registration accepted a password, anyone could register a victim's address
with a password they chose; if the victim later clicked the verification link,
the attacker could sign in. Registration therefore only records the email. The
account gets a password only when the inbox owner follows the link and sets it,
and the token is re-checked under a row lock so it works exactly once.

### Anti-enumeration

Registration, verification resend and password reset give the same response
whether or not an address is registered. Registering an existing ACTIVE address
emails its owner instead; a PENDING address receives a fresh activation link;
SUSPENDED and DISABLED addresses receive nothing. Login errors are generic.

### Recovery

| Status | Password reset | Activation/confirmation link |
| --- | --- | --- |
| PENDING | No (no password yet) | Yes: resend activation |
| ACTIVE | Yes (1 h, single use; ends all sessions) | If email unverified |
| SUSPENDED / DISABLED | No | No |

## Roles and permissions

| Capability | MEMBER | CONTENT_MANAGER | SUPER_ADMIN |
| --- | :---: | :---: | :---: |
| Member pages (when verified) | ✔ | ✔ | ✔ |
| Django admin sign-in | ✘ | ✔ (`is_staff`) | ✔ |
| Account-management permissions by default | ✘ | ✘ | ✔ (superuser) |
| Manage MEMBER accounts | ✘ | only if a superuser grants user permissions | ✔ |
| Manage staff or own account in admin | ✘ | ✘ | ✔ |
| Change roles, groups, permissions, email verification | ✘ | ✘ | ✔ |
| Reactivate DISABLED accounts | ✘ | ✘ | ✔ |
| View/edit Groups | ✘ | ✘ | ✔ |

* `is_staff`, `is_superuser` and `is_active` are derived from role and status and
  cannot be set directly (model, manager, admin and database constraints).
* Each role has a Django Group (`Super Administrators`, `Content Managers`,
  `Members`) kept in sync automatically. Content Managers receive content
  permissions when content models exist (Phase 04).
* The last ACTIVE Super Administrator cannot be demoted, suspended, disabled or
  deleted; concurrent changes are serialized with row locks.
* Superusers are created only with `manage.py createsuperuser`.

## Rate limits

| Endpoint | Limits |
| --- | --- |
| Login (public and admin) | 5 failures / 15 min per account+IP; 50 / hour per account; 50 / 15 min per IP |
| Registration | 10 / hour per IP |
| Registration, verification resend, password reset emails | 3 / hour per address; 20 / hour per IP |

Per account+IP limiting means an attacker cannot lock a victim out from another
network; the per-account ceiling still stops distributed guessing. Limits use
the Django cache. With `DJANGO_THROTTLE_REQUIRE_SHARED_CACHE` (default on when
`DEBUG` is off) a per-process cache fails the startup check `accounts.E001`, and
limits fail closed on cache errors. `X-Forwarded-For` is ignored unless
`DJANGO_TRUSTED_PROXY_COUNT` is set.

## Security logging

Sign-in, registration, activation, password and access-change events are
permanent audit records (see [audit.md](audit.md)); each also writes one
`clipcash.security` log line with ids and codes only. Registration and email
request throttling remain log-only operational signals.
