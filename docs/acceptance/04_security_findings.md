# 04 — Application Security Findings (Phase 13D)

Defensive, source-based review. No penetration testing, fuzzing or live-environment probing was performed.
**No exploitable P0 vulnerability was identified.** Findings below are classified Confirmed (code evidence) /
Suspected (plausible, not demonstrated) / Unverified (depends on an environment not available to the audit).

## 1. Authentication and sessions

| Control | Evidence | Assessment |
| --- | --- | --- |
| Password storage | Django default PBKDF2 in production; MD5 only inside the test runner ([config/test_runner.py:34-47](../../config/test_runner.py#L34-L47)) | ✅ |
| Password policy | min length 10, similarity, common, numeric validators ([config/settings.py:172-186](../../config/settings.py#L172-L186)) | ✅ |
| Registration | Email-only; password chosen by the mailbox owner at activation, so nobody can pre-set another person's password ([apps/accounts/forms.py:29-39](../../apps/accounts/forms.py#L29-L39)) | ✅ |
| Account enumeration | Registration, resend and reset return identical responses whether or not the address exists; throttles applied silently ([apps/accounts/views.py:76-87, 222-240](../../apps/accounts/views.py#L76-L87)) | ✅ |
| Tokens | Separate salts for verification (24 h) and reset (1 h); hash includes password, last login, email, status and `email_verified`, so tokens die on any state change ([apps/accounts/tokens.py](../../apps/accounts/tokens.py)) | ✅ |
| Token leakage | Activation token moved into the session (Django reset pattern); Gunicorn access log off; log filter redacts token paths ([config/gunicorn.conf.py:125-129](../../config/gunicorn.conf.py#L125-L129), `apps/core/log_filters.py`) | ✅ |
| Email confirmation | GET shows a button; only POST confirms (link scanners cannot confirm) ([views.py:124-151](../../apps/accounts/views.py#L124-L151)) | ✅ |
| Login rate limiting | per account+IP 5/15 min, per account 50/h, per IP 50/15 min; fail-closed on misconfiguration ([apps/accounts/throttling.py](../../apps/accounts/throttling.py)) | ⚠️ see **CC-004** |
| Inactive accounts | `AccountStatusBackend` rejects non-ACTIVE at login **and** on every request; middleware flushes stale sessions | ✅ |
| Session cookies | HttpOnly, SameSite=Lax, Secure by default outside DEBUG; 12 h age; `check --deploy` errors if not secure ([apps/accounts/checks.py:25-34](../../apps/accounts/checks.py#L25-L34)) | ✅ |
| Logout | POST-only with CSRF | ✅ |
| Privilege escalation via admin | Only superusers may create staff, change roles, groups, permissions or `email_verified`; staff cannot edit themselves; last super admin protected ([apps/accounts/admin.py:15-31](../../apps/accounts/admin.py#L15-L31), [services.py:178+](../../apps/accounts/services.py#L178)) | ✅ |
| Demo email waiver | Allow-list by email; only with restricted demo **and** no payment of any kind possible; never marks the email verified; financial/advertiser services still require verification ([apps/accounts/access.py:13-52](../../apps/accounts/access.py#L13-L52)) | ✅ (operational: remove before public launch — documented) |
| MFA for staff | Not implemented | ⚠️ recommended before production (part of CC-012 batch) |

## 2. Authorization

| Area | Evidence | Assessment |
| --- | --- | --- |
| Role-based access | `RoleRequiredMixin` / `VerifiedAccountRequiredMixin`; staff console via `staff_member_required` | ✅ |
| Staff-only operations | Every `/manage/` view checks a specific permission; every POST also passes through a service that re-checks (`_require`, `_staff_with(... has_perm ...)`) | ✅ defence in depth (verified for advertisers, campaigns, viewing, rewards, payments, wallets, withdrawals) |
| Organisation isolation | `organizations_for(user)` → `organization_for` / `campaign_for` filter by active membership; staff accounts get no organisations ([apps/advertising/services.py:131-158](../../apps/advertising/services.py#L131-L158)) | ✅ |
| Cross-user object access (IDOR) | All member-facing lookups are scoped: subscriptions `member=request.user`, intents via `intent_for`, sessions `member=member`, demo withdrawals `member=request.user`, wallet transactions `wallet=wallet`, allocations `organization=organization` | ✅ no IDOR found |
| Service-level authorisation | Services take `actor`/`user` and re-validate; ledger `_authorize` permits only `actor=None` (system) or active superusers ([apps/wallets/services.py:76-84](../../apps/wallets/services.py#L76-L84)) | ✅ |
| Background-job assumptions | Jobs run as system (`actor=None`); they only call services that are themselves feature-gated and DB-guarded | ✅ |
| Own-campaign viewing | Advertiser team members cannot watch their own campaigns ([apps/viewing/services.py:189-190](../../apps/viewing/services.py#L189-L190)) | ✅ |
| Staff acting as advertiser | Refused (`staff_account`) ([advertising/services.py:108-115](../../apps/advertising/services.py#L108-L115)) | ✅ |
| Separation of duties | Four-eyes on reward policies, refunds, demo withdrawals | ✅ in code; operational dependency CC-012 |

## 3. Web security

| Control | Evidence | Assessment |
| --- | --- | --- |
| CSRF | Middleware on; JSON viewing endpoints send `X-CSRFToken`; only two CSRF-exempt views, both HMAC-authenticated sandbox callbacks | ✅ |
| XSS | Django autoescape; **no** `|safe`, `mark_safe` or `autoescape off` in templates/app code; **no** inline `<script>`, inline `style=` or `on*=` handlers | ✅ |
| CSP | `default-src 'self'`, `script-src 'self'`, `style-src 'self'`, `object-src 'none'`, `frame-ancestors 'none'`, `form-action 'self'`; Bunny hosts only where needed ([apps/core/csp.py](../../apps/core/csp.py)); Django admin excluded (uses inline scripts) | ✅ |
| SQL injection | ORM throughout; raw SQL limited to migrations and fixed statements (`SET CONSTRAINTS`, `SET LOCAL clipcash.audit_purge`) | ✅ |
| Unsafe redirects | Django `LoginView` validates `next`; `set_language` validates `next` | ✅ |
| File uploads | Videos upload directly to Bunny via signed TUS; content types and size limited in settings; no user file upload to `MEDIA_ROOT` found | ✅ (Bunny-side validation unverified) |
| Security headers | `SecurityMiddleware` (nosniff, Referrer-Policy same-origin, COOP), `X-Frame-Options` DENY, CSP; HSTS 0 by default pending HTTPS verification (CC-027) | ✅ / ⚠️ |
| Debug | `DEBUG` defaults False; sandbox payments/demo video refused by deploy checks outside DEBUG unless explicitly allowed | ✅ |
| Host validation | `ALLOWED_HOSTS` default localhost; Render hostname appended; health probe intentionally answers before host validation and reveals only `ok`/`unavailable` ([apps/core/middleware.py:16-42](../../apps/core/middleware.py#L16-L42)) | ✅ |
| Error disclosure | Custom 400/403/404/500 templates; DB errors in health check logged by class name only | ✅ |

## 4. Integration security

| Control | Evidence | Assessment |
| --- | --- | --- |
| Bunny credential handling | API key only in `AccessKey` header server-side; token key separate; `__repr__` hides secrets ([apps/videos/providers/bunny.py](../../apps/videos/providers/bunny.py)) | ✅ |
| Redirect behaviour | Custom opener refuses redirects so the API key is never forwarded | ✅ |
| Timeouts | Bunny 10 s; SMTP 20 s; Gunicorn 60 s | ✅ |
| Signed playback URLs | SHA-256 embed token, 600 s; CDN HS256 thumbnails | ✅ / ⚠️ tokens not bound to the viewer; HLS segment protection depends on Bunny CDN token auth being enabled (CC-025, environment) |
| Callback signature | HMAC-SHA256 with `salted_hmac` over the raw body; `compare_digest` ([apps/payments/providers/sandbox.py:78-95](../../apps/payments/providers/sandbox.py#L78-L95)) | ✅ (sandbox) |
| Replay prevention | Unique event id → DUPLICATE; callback body carries no amounts/statuses; authoritative status fetched | ✅ |
| Timestamp validation | None — acceptable because a notice only triggers a re-fetch of authoritative state | ✅ by design; a real provider must be re-assessed |
| Idempotency | Intents, postings, demo withdrawals, ledger entries, outbox jobs | ✅ |
| Secret separation | Sandbox callback key derived from `SECRET_KEY` (sandbox only) | ⚠️ a real provider must use its own secret (future) |
| Callback flood handling | Throttle is one **global** bucket per provider; an unauthenticated request inserts a `ProviderEvent` row before rejection ([apps/payments/services.py:302-327](../../apps/payments/services.py#L302-L327)) | ⚠️ **CC-020** (P3; sandbox only, endpoint 404 when payments disabled) |

## 5. Audit and accountability

| Aspect | Evidence | Assessment |
| --- | --- | --- |
| Coverage | Business and security events for auth, accounts, memberships, advertising, viewing, rewards, payments, ledger, wallets, withdrawals; denied actions recorded | ✅ |
| Atomicity | Business events written in the same transaction as the change (e.g. ledger posting) | ✅ |
| Application immutability | `AuditEvent.save/delete` raise `AuditImmutableError` ([apps/audit/models.py:18-27](../../apps/audit/models.py#L18-L27)) | ✅ |
| Database immutability | Trigger blocks UPDATE/DELETE unless `clipcash.audit_purge='on'` ([apps/audit/migrations/0002_append_only_trigger.py](../../apps/audit/migrations/0002_append_only_trigger.py)) | ⚠️ the GUC can be set by **any** role, and TRUNCATE is not blocked; real protection needs the documented role separation (**CC-005**) |
| Tamper evidence | No hash chaining / external log shipping | ⚠️ recommended for production (CC-016) |
| Privacy | Emails/passwords/tokens not logged; invitee address not written to audit; IP/UA recording configurable | ✅ |

## 6. Security findings

| ID | Sev | Title | Confidence |
| --- | --- | --- | --- |
| CC-004 | P1 | Per-IP throttles become site-wide behind Render's proxy while `DJANGO_TRUSTED_PROXY_COUNT=0`; 50 failed logins from anyone in 15 min would block **all** logins ([apps/accounts/forms.py:52-58](../../apps/accounts/forms.py#L52-L58), [throttling.py:110-133](../../apps/accounts/throttling.py#L110-L133)) | Confirmed in code; impact environment-dependent (Render B3) |
| CC-005 | P1 | All database guarantees (ledger, wallet lock, withdrawal lock, audit append-only) can be disabled by the single owner credential the app uses on Render; audit purge GUC settable by any role | Confirmed (documented B6) |
| CC-012 | P2 | Only three roles; finance/review permissions granted ad hoc by superusers; no staff MFA | Confirmed |
| CC-014 | P2 | Viewing anti-fraud trusts client-reported positions (bounded by server time); no device/IP/multi-account signals | Suspected risk (inherent) |
| CC-016 | P2 | No error monitoring, alerting or off-host audit/log retention | Confirmed |
| CC-020 | P3 | Sandbox callback endpoints persist unauthenticated attempts and share one throttle bucket | Confirmed (sandbox only) |
| CC-025 | P3 | Playback tokens not viewer-bound; HLS protection depends on Bunny dashboard setting | Unverified (environment) |
| CC-027 | P3 | HSTS off by default until HTTPS verified | Confirmed (documented) |

## 7. Limitations

* No dynamic testing (DAST), dependency vulnerability scan (no `pip-audit` installed — installing tools was out of
  scope) or secret scan of Git history was run. The `.env` file is git-ignored and was not committed
  (`git check-ignore .env`); its values were not read into this report.
* Bunny Stream and SMTP behaviour were assessed from code and mocked tests only.
