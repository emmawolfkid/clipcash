# Phase 14-A — Complimentary Demo Membership Activation and Staff Permission Provisioning

**Status:** implementation and corrective batch (review findings R-01–R-04, R-07) complete, awaiting owner review
(not committed) · **Baseline:** `main` @ `a87c81b`. The corrective batch is described in §14; earlier sections are
updated to the corrected behaviour.
**Owner decision implemented:** D1 Option A — time-limited complimentary demo memberships.
**Audit issues addressed:** CC-001 (no in-product membership activation), CC-024 (grant skipped eligibility),
CC-012 (provisioning part only).

## 1. Existing architecture reused

| Component | Use |
| --- | --- |
| `memberships.services.grant_complimentary` | The single complimentary-grant service (refactored; no second activation path) |
| `_lock_member`, `_current_and_queued`, `_new_pending`, `_activate`, `_transition` | Member row lock, live-membership check, PENDING snapshot, database-enforced PENDING→ACTIVE transition and its audit event |
| `member_eligibility` (without the demo waiver) | Account rules: ACTIVE and verified email |
| `memberships.access.check_access` | Unchanged authoritative access decision (`starts_at <= now < expires_at`, read from the database every time) |
| `process_expiry` job | Unchanged; marks ended periods EXPIRED |
| `Subscription` columns | `activation_source='COMPLIMENTARY'`, `amount_paid=0`, `payment_reference=''` (check constraint `sub_activation_source_consistent`), `activated_by_id`, `expires_at` (frozen by trigger), `note` (reason; previously unused) |
| `audit.services.record_business_event` + `AuditEvent.idempotency_key` | Grant audit record in the same transaction; unique key gives retry idempotency |
| `is_super_admin` | Authority check (active superuser with role SUPER_ADMIN) |
| `/manage/members/` directory and member detail | Locating the member; entry point to the grant |
| `accounts.roles` (`ROLE_GROUP_NAMES`, `ROLE_PERMISSIONS`) | Source of truth for provisioning |

## 2. New behaviour

* **Console workflow** `/manage/members/<id>/complimentary/`: choose an ACTIVE plan, an expiry (`datetime-local`,
  site time zone, shown on the page), and a reason → **Review** (no change) → tick "no payment was received or
  simulated" → **Grant**. Success message states the expiry and "No payment was recorded". The member detail
  page shows the button only to authorised Super Administrators and labels complimentary memberships.
* **Member pages**: dashboard card and *My membership* label the membership "Complimentary … no payment".
* **CLI** `grant_complimentary_membership` now requires `--expires-at` (ISO 8601 with offset) and `--reason`,
  accepts `--operation-id`, and calls the same service. The DEBUG-only `--dev-period-minutes` option was removed
  (replaced by the explicit expiry).
* **Provisioning** `provision_staff_permissions` (new): preview by default; `--apply` adds missing role
  permissions and role-group memberships; `--remove-stale` (with `--apply`) removes accounts from groups of a
  role they no longer hold.

## 3. Files changed

| File | Change |
| --- | --- |
| `apps/memberships/services.py` | `grant_complimentary` rewritten (expiry, reason, operation id, eligibility, conflicts, audit); `complimentary_grants_available`, `can_grant_complimentary`; `_activate(note=...)` writes the reason in the same UPDATE |
| `apps/memberships/forms.py` | `ComplimentaryGrantForm`, `ComplimentaryConfirmForm` |
| `apps/memberships/management/commands/grant_complimentary_membership.py` | New arguments; same service |
| `apps/administration/views.py`, `apps/administration/urls.py` | `ComplimentaryGrantView` (`administration:member_complimentary`); `can_grant` on member detail |
| `templates/administration/members/complimentary.html` (new) | Grant / review page |
| `templates/administration/members/detail.html` | Grant button (authorised only); complimentary labels |
| `templates/members/dashboard.html`, `templates/memberships/my_membership.html` | Complimentary labels for members |
| `static/css/clipcash.css` | `datetime-local` input styled like other console inputs |
| `apps/audit/events.py` | Registered `membership.complimentary.granted` and `authorization.roles.provisioned` |
| `apps/accounts/management/__init__.py`, `.../commands/__init__.py`, `.../commands/provision_staff_permissions.py` (new) | Provisioning command |
| `apps/memberships/tests/test_complimentary.py` (new, 43 tests) | Service, expiry, idempotency and retry-key binding, replay of ended grants, concurrency (6 multi-connection tests), demo waiver, financial invariants, CLI |
| `apps/administration/tests/test_complimentary_console.py` (new, 12 tests) | Console, authorisation, CSRF, member pages, resubmission feedback |
| `apps/accounts/tests/test_provisioning.py` (new, 11 tests) | Provisioning command incl. fail-closed elevation checks |
| `apps/memberships/tests/test_lifecycle.py` | Removed 3 tests of the old signature (superseded by `test_complimentary.py`) |
| `apps/notifications/tests/test_outbox.py` | R-07: deterministic marker for the "SMTP server text is never stored" assertion (test-only) |
| `docs/architecture/memberships.md` | Complimentary-grant section and audit list updated |
| `docs/implementation/phase_14a_complimentary_membership.md` (new) | This document |

## 4. Authorisation

A grant succeeds only if **all** hold, checked inside `grant_complimentary` (so every interface is covered):

1. `MEMBERSHIPS_COMPLIMENTARY_GRANTS_ENABLED` (`DJANGO_MEMBERSHIPS_COMPLIMENTARY_GRANTS=True`) **and**
   `RESTRICTED_DEMO_MODE` (`DJANGO_RESTRICTED_DEMO`, default on outside DEBUG). Either off → `grants_disabled`.
2. The actor is an **active Super Administrator** (`is_superuser` and role `SUPER_ADMIN`). `is_staff`, Django
   permissions (including `memberships.manage_memberships`) and group membership are never sufficient, and no new
   assignable permission was introduced.
3. The `memberships` feature is enabled.

The console view adds `staff_member_required` and returns 403 to anyone for whom `can_grant_complimentary` is
false; the member page hides the button for them. Members and anonymous users are redirected to the admin login.
CSRF protects both POST steps. Refusals are recorded as `membership.action.denied` security events
(`action=subscription.grant`, reason code).

## 5. Grant validation

| Rule | Error |
| --- | --- |
| Member and plan given | `ValidationError` |
| Reason 5–200 characters after whitespace normalisation | `ValidationError` (`reason`) |
| Operation id, if given, 8–64 letters/digits/hyphens | `ValidationError` |
| Target account role is MEMBER (staff and super admins excluded; no self-grant) | `NotEligible` `not_a_member` |
| Member eligible: ACTIVE, verified email (no demo waiver) | `NotEligible` (`account_inactive` / `email_unverified`) |
| Plan ACTIVE (INACTIVE/ARCHIVED refused; non-public active plans allowed) | `PlanUnavailable` |
| Expiry timezone-aware, after now, ≤ now + plan duration | `ValidationError` (`expires_at`) |
| No current, scheduled (queued) or suspended membership | `NotEligible` `membership_exists` — existing membership untouched |
| Pending choice of a **different** plan | `InvalidTransition` `pending_choice_conflict` — choice untouched |
| Pending choice of the same plan | Used for the grant |
| Pending choice past its deadline | Cancelled `UNPAID_TIMEOUT` (the outcome `process_expiry` would apply), then a new one is created |
| Repeated operation id, identical request (same administrator, member, plan, expiry instant, normalised reason), grant still in force | Returns the original subscription unchanged |
| Repeated operation id, any input different | `InvalidTransition` `operation_conflict` with a generic message ("This form was already used for a different grant…") that describes nothing about the original |
| Repeated operation id, identical request, grant cancelled, suspended or past its expiry | `InvalidTransition` `grant_not_active` naming the state; nothing re-created or reactivated |

### Concurrency design

1. **Retry key** (when given): `SELECT pg_advisory_xact_lock(hashtextextended(key, 0))` is the first statement of
   the grant transaction. Every request carrying the same key waits for the previous one to commit or roll back,
   whichever member or plan it names, across all processes and servers; the lock is released automatically at
   transaction end. A hash collision only makes two unrelated keys wait for each other.
2. **Member**: `SELECT … FOR UPDATE` on the member row (the existing `_lock_member`), shared by every membership
   change (choices, payments, grants, expiry is per-row). Lock order is always key → member; other membership
   operations take only the member lock, so no lock cycle exists.
3. **Replay check** runs after both locks, against committed data.
4. **Audit integrity check**: after writing `membership.complimentary.granted`, the service requires the stored
   event to target this subscription. The audit layer answers a duplicate idempotency key with the *existing*
   event; if that ever happened the grant raises `operation_conflict` and the whole transaction (pending row,
   activation, all events) rolls back. This second layer is tested on its own with the advisory lock disabled.
5. The exclusion constraint `sub_no_overlapping_periods` remains the database backstop for overlapping periods
   (an `IntegrityError` is converted to a refused `conflict`).

No migration was needed: advisory locks are a PostgreSQL feature and the project runs on PostgreSQL only.

## 6. Expiry handling

The period is exactly `[grant time, chosen expiry)`: `_activate` is given `period = expires_at - now` and no
other membership is live, so `starts_at = now` and `expires_at` equals the chosen instant (tested, including a
`+03:00` input). Access is decided by `check_access` on every request (`starts_at <= now < expires_at`), so access
ends at the expiry even if the `memberships.process_expiry` job has not yet run; the job then marks the row
EXPIRED. No new scheduler was added. Sponsored viewing and playback re-check `check_access` (via
`campaign_eligibility` and `authorize_playback`) at start and on every heartbeat.

## 7. Audit events

| Event | Kind | When | Metadata |
| --- | --- | --- | --- |
| `membership.subscription.created` | business | new PENDING created for the grant | plan, list price, renewal=false |
| `membership.subscription.activated` | business | PENDING→ACTIVE | from/to status, plan, `source=COMPLIMENTARY` |
| `membership.complimentary.granted` (new) | business, idempotency key `membership.complimentary:<operation>` | successful grant | member id, plan code, starts_at, expires_at, reason, operation |
| `membership.subscription.cancelled` | business | stale pending cancelled | reason `UNPAID_TIMEOUT` |
| `membership.action.denied` | security (survives rollback) | any refused grant | action `subscription.grant`, reason code |
| `authorization.roles.provisioned` (new) | business | `provision_staff_permissions --apply` | counts added/removed |

Actor, request id, IP and user agent come from the existing audit context. No secrets or email addresses are
added to metadata. A test proves the grant rolls back if its audit write fails.

## 8. Staff permission provisioning

```text
python manage.py provision_staff_permissions                         # preview, changes nothing
python manage.py provision_staff_permissions --apply                 # add missing role permissions / role groups
python manage.py provision_staff_permissions --apply --remove-stale  # also remove stale role-group memberships
```

* Provisions only the roles that exist: Super Administrators (no explicit grants; superusers), Content Managers
  (`videos.manage_videos`, `videos.publish_videos`, `videos.manage_categories`), Members (none).
* Never creates accounts, sets passwords, changes roles or staff/superuser flags, or removes permissions from
  groups.
* **Fails closed (R-04):** any permission on a role group outside that role's approved list, or a missing role
  permission, is shown as `! SECURITY` in the preview, and `--apply` then raises an error and changes nothing.
  Unapproved permissions are never removed automatically; a Super Administrator resolves them in Django admin.
  The decision is repeated inside the apply transaction after locking the role-group rows (`SELECT … FOR UPDATE`),
  so a group changed after the preview is caught; any refusal rolls back every change.
* Adding an account to its role group can therefore only grant permissions from `ROLE_PERMISSIONS`. Permissions
  the account already holds directly or through other groups are left exactly as they are.
* Preview mode issues no INSERT, UPDATE or DELETE (verified by capturing all SQL in a test).
* Reports how many active staff hold each separation-of-duties permission.

**Gap reported, not filled:** Finance Officer, Support Officer and Auditor **are not defined roles** in this
code base. The read-only preview against the local development database shows **one** holder (the super
administrator) for every separation-of-duties permission, so two-person workflows (reward policies, refunds,
wallet restriction lifts, demo withdrawals) cannot be demonstrated until a Super Administrator grants those
permissions to additional named staff through Django admin, or new roles are approved (decision D2).

## 9. Database migration status

**No migration was created or changed.** The grant uses existing columns and constraints; the new audit actions
are registry entries (the `MEMBERSHIP` and `AUTHORIZATION` categories already exist in the
`audit_category_valid` constraint). `makemigrations --check --dry-run` → "No changes detected".

## 10. Financial integrity assessment

The grant writes only `memberships_subscription` and `audit_auditevent`. It does not import or call payments,
sandbox, ledger, wallet, rewards, advertising-funding or withdrawal code.
`FinancialInvariantTests.test_grant_creates_no_money_records_and_leaves_locks_installed` snapshots
PaymentIntent, ProviderEvent, SandboxPosting, SandboxAdvertiserFunds, SandboxCampaignAllocation,
MembershipActivationPreview, JournalEntry, JournalLine, WalletTransaction, DemoReward, CampaignRewardBudget,
WithdrawalRequest, DemoWithdrawal, every ledger account balance and campaign funding status before and after a
grant (identical), confirms wallet balances stay 0 and real withdrawal eligibility stays `withdrawals_locked`,
and checks that 13 lock triggers are installed and enabled plus the `adv_campaign_funding_locked` and
`sub_activation_source_consistent` constraints exist. A second test proves the database refuses any later attempt
to give a complimentary row an amount, a payment reference, a PAYMENT source or a longer expiry.

| # | Invariant | Effect of 14-A |
| --- | --- | --- |
| 1 | Membership fees never fund rewards | No fee exists (amount 0); no ledger entry |
| 2 | Real rewards need reconciled advertiser funding | Untouched |
| 3 | Sandbox never becomes real | No sandbox record created |
| 4 | Simulated rewards non-withdrawable | Untouched |
| 5 | Demo budgets never real funds | Untouched |
| 6 | Real wallet balances TZS 0 | Verified 0 after grant |
| 7 | Wallet posting DB-blocked | Trigger verified installed |
| 8 | Real withdrawals DB-blocked | Trigger verified; eligibility still locked |
| 9 | Real advertiser funding disabled | Constraint verified |
| 10 | No real transfers | No provider called |
| 11 | Ledger rules | No entry; triggers verified |
| 12 | Idempotency | Grant is idempotent by operation id, bound to the full request and serialised by an advisory lock |
| 13 | Financial permissions | No permission added or broadened |
| 14 | DB-enforced restrictions | No migration, trigger or constraint changed |
| 15 | Isolated tests, no transfers | All tests on `test_clipcash_db`; no external calls |

## 11. Automated test results

Test database isolation re-confirmed: development `clipcash_db`, tests `test_clipcash_db`; the project runner
refuses any other target. Python 3.13.14, Django 5.2.18, PostgreSQL 18.1. Run 2026-11-03.

| Command | Result |
| --- | --- |
| `manage.py test apps.memberships.tests.test_complimentary --keepdb` | 36 run, OK |
| `manage.py test apps.memberships.tests.test_complimentary.ComplimentaryConcurrencyTests --keepdb` (×3, multi-connection threads) | 4 run, OK each time |
| `manage.py test apps.administration.tests.test_complimentary_console --keepdb` | 10 run, OK |
| `manage.py test apps.accounts.tests.test_provisioning --keepdb` | 6 run, OK |
| `python -W default manage.py test apps --keepdb -v 2` (full suite, original implementation) | **909 run, 909 passed, 0 failures, 0 errors, 0 skipped; 249.5 s** (254 s wall). After the corrective batch: **923 run, all passed** (§14) |
| `manage.py check` | No issues |
| `manage.py makemigrations --check --dry-run` | No changes detected |
| `manage.py showmigrations` | 58 applied, 0 pending (unchanged) |

Test count: 860 before → 909 after (+52 new, −3 superseded old-signature complimentary tests in
`test_lifecycle.py`). Warnings are the pre-existing ones only: `RemovedInDjango60Warning` (URLField scheme, CC-022),
an unclosed-socket `ResourceWarning` in a Bunny provider mock test, and the expected `UserWarning` from the runner
guard test. During development two of my own new test assertions were wrong and were corrected (an existing
paid membership in one validation test; the console reads times in the site time zone, Africa/Dar_es_Salaam);
no application defect was found by them.

## 12. Manual acceptance checklist

Run on a **local** environment by a person. Nothing in this checklist is automated against the development
database, and no account is created by the code. Record evidence (screenshot name, audit event id) per step.

### 12.1 Preparation through existing workflows

| # | Step | Expected |
| --- | --- | --- |
| P1 | Sign in to `/admin/` as the Super Administrator → *Site configuration*: enable **Memberships** | Saved; audit event for the configuration change |
| P2 | `/admin/` → *Membership plans* → add an ACTIVE plan (e.g. `demo-monthly`, TZS 5,000, 30 days, entitlement `videos.standard`) | Plan listed as Active |
| P3 | Create **two** test members (A, B) with the existing registration flow: with `DJANGO_RESTRICTED_DEMO` **off**, register at `/accounts/register/`; the activation link is printed by the console email backend (worker: `python manage.py run_jobs`); open it and set a password | Each account ACTIVE with a confirmed email (activation confirms it). Alternative: Super Administrator creates the account in `/admin/` → Users and ticks "email verified" |
| P4 | Optional (unauthorised-staff steps): Super Administrator creates a Content Manager account in `/admin/` | Content Manager can sign in to `/manage/` |
| P5 | Set `DJANGO_RESTRICTED_DEMO=True` and `DJANGO_MEMBERSHIPS_COMPLIMENTARY_GRANTS=True` in `.env`; restart the server | Restricted-demo banner on the homepage; registration page now closed |
| P6 | `python manage.py provision_staff_permissions` (preview only — do **not** use `--apply` on shared data) | No `! SECURITY` lines; undefined roles listed; holder counts shown |

### 12.2 Grant, display and eligibility

| # | Step | Expected |
| --- | --- | --- |
| 1 | As Super Administrator open `/manage/members/`, search member A, open it | "Grant complimentary demo membership" button visible |
| 2 | Open the grant page | Warning that this is not a payment; member status, email confirmation, current membership and **time zone** (Africa/Dar_es_Salaam by default) shown |
| 3 | Submit with a past expiry, then with a 3-character reason | Clear field errors; nothing granted |
| 4 | Enter the plan, expiry tomorrow 18:00, a reason → **Review** | Summary: plan, "Access ends" tomorrow 18:00 **with the time-zone name**, reason, "TZS 0 — complimentary"; nothing granted yet |
| 5 | **Grant** without ticking the box | "Tick the confirmation box" error |
| 6 | Tick the box → **Grant** | Redirect to member page; success message with expiry and "No payment was recorded"; membership "Complimentary (no payment)" and the same date/time as entered |
| 7 | `/admin/` → Audit events | `membership.complimentary.granted`: actor = you, member id, plan, start, expiry (UTC offset), reason, operation id |
| 8 | Sign in as member A | Dashboard card and *My membership* show plan, "Valid until …" and the complimentary label; video library no longer reports "no membership" (playback itself needs Phase 14-C) |

### 12.3 Duplicates, retries and existing memberships

| # | Step | Expected |
| --- | --- | --- |
| 9 | Grant to member B; on the confirm page **double-click** "Grant" | One membership; one `membership.complimentary.granted` event |
| 10 | Open the grant page for member B in **two tabs**, review both, confirm in tab 1 then tab 2 | Tab 2 refused: "already has a current or scheduled membership" |
| 11 | After a successful grant, press **Back** to the confirm page and resubmit unchanged | Redirect to the member page again (same grant returned); still one membership |
| 12 | Press Back, **edit** the reason or expiry on the review page and resubmit | Refused: "This form was already used for a different grant…"; the original grant is unchanged |
| 13 | Cancel member B's grant in `/admin/` → Subscriptions (action *cancel*, reason) then resubmit the old confirm page | Refused: "…no longer active (cancelled)…"; no success message; nothing re-created |
| 14 | Try a new grant for member A (already granted) | Refused: already has a current or scheduled membership; existing membership unchanged |

### 12.4 Unauthorised and expired sessions

| # | Step | Expected |
| --- | --- | --- |
| 15 | Signed out, open `/manage/members/<A>/complimentary/` | Redirect to the admin sign-in page |
| 16 | Signed in as member A, open the same URL | Redirect to the admin sign-in page; nothing about the member shown |
| 17 | Signed in as the Content Manager: open member A | No grant button; the grant URL returns 403 (generic page) |
| 18 | As Super Administrator, open Review, then sign out in another tab (or wait past `DJANGO_SESSION_COOKIE_AGE`) and confirm | Redirect to sign-in or "CSRF verification failed" (403); nothing granted |
| 19 | Set `DJANGO_RESTRICTED_DEMO=False`, restart | Button gone; URL 403; CLI refused (`grants_disabled`) |

### 12.5 Expiry

| # | Step | Expected |
| --- | --- | --- |
| 20 | Create member C (P3); grant with expiry ~5 minutes ahead | Granted; dashboard shows the expiry time |
| 21 | Wait past the expiry **without** running the worker; reload member C's dashboard | Membership no longer current immediately |
| 22 | Run `python manage.py run_jobs`; reload `/manage/members/<C>/` | Subscription EXPIRED; `membership.subscription.expired` audit event |

### 12.6 Accessibility and layout (browser)

| # | Step | Expected |
| --- | --- | --- |
| 23 | Keyboard only on the grant page: Tab through plan, date/time, reason, Review, checkbox, Grant | Visible focus on every control; logical order; checkbox toggles with Space; no keyboard trap |
| 24 | Screen reader (NVDA/VoiceOver) | Warning note announced; each field announced with its label; errors announced (role=alert); checkbox label read in full |
| 25 | Browser width 360 px | No horizontal scrolling; inputs full width; buttons reachable |
| 26 | Date/time control in each browser used for the demo | Value entered equals the value shown on the review page and on the member page |

### 12.7 Financial non-movement

| # | Step | Expected |
| --- | --- | --- |
| 27 | Before step 6 and after step 22 note counts in `/admin/`: Journal entries, Wallet transactions, Withdrawal requests, Payment intents, Sandbox postings, Demo rewards | All counts unchanged |
| 28 | Member A's `/wallet/` | TZS 0 in every balance |
| 29 | `/manage/health/` | Still states payments are disabled |
| 30 | `/admin/` → Campaigns | Every funding status UNFUNDED |

## 13. Remaining risks

* **Separation of duties** still needs additional named staff with explicit permissions (D2); documented above.
* **Demo accounts using the email-verification waiver cannot receive grants** (existing eligibility rule kept by
  design). For a demo, give the demo member a confirmed email (Super Administrator via Django admin) or verify by email.
* **Local demos** require `DJANGO_RESTRICTED_DEMO=True`, which also closes public registration locally.
* A grant's `duration_days` snapshot shows the plan's list duration; the real period is `starts_at`–`expires_at`
  (pages display the expiry).
* No browser-level test of the new page (CC-015 remains open); §12.4–12.6 must be executed by a person.
* `provision_staff_permissions --apply` has not been run on any shared database; it is safe to preview anywhere.

## 14. Corrective batch (final review findings R-01 – R-04, R-07)

The findings are recorded unchanged in `phase_14a_final_acceptance_review.md`; this section records the fixes.

| Finding | Root cause | Correction | Files |
| --- | --- | --- | --- |
| **R-01** concurrent reuse of one retry key for two members → two grants, one grant audit event | Grants were serialised only by the **member** row lock, so two members ran in parallel; the second grant's audit insert hit the unique `idempotency_key`, and `audit.services._insert` treats that as an idempotent replay, returning the first member's event while the second activation committed | (1) PostgreSQL transaction-level **advisory lock on the key** taken before the member lock; (2) the service verifies that the grant audit event it receives targets this subscription, otherwise raises `operation_conflict` and rolls back everything. Ownership rule: one key = one grant request | `apps/memberships/services.py` (`_lock_operation`, `grant_complimentary`) |
| **R-02** key bound only to member + plan | `_replayed_grant` compared two fields | The key is bound to the administrator (event actor), member, plan, expiry instant and normalised reason, and to the operation type (`membership.complimentary.granted`); any difference → `operation_conflict` with a generic message | `apps/memberships/services.py` (`_replayed_grant`, `OPERATION_CONFLICT`) |
| **R-03** replay of a cancelled grant returned it as success | Replay returned the stored subscription regardless of state | Replay returns the grant only while `is_in_force(now)`; otherwise `grant_not_active` naming the state (cancelled, suspended, ended). Never reactivates or re-creates; audit history untouched | `apps/memberships/services.py` |
| **R-04** provisioning `--apply` could add accounts to a role group carrying extra permissions | The command treated extra group permissions as information and still added members to the group | Fail closed: unapproved group permissions or missing role permissions make `--apply` refuse with no changes; the check is repeated inside the transaction after locking the role groups; nothing is removed automatically | `apps/accounts/management/commands/provision_staff_permissions.py` |
| **R-07** flaky `test_transient_smtp_failure_retries_then_fails_terminally` | The invariant "the SMTP server's text is never stored" was checked with the marker `abc`, which random hex ids can contain | Marker `SMTP_SERVER_TEXT_Qz7Kx` (upper case, `_`, letters beyond `f`) cannot occur in stored hex ids, UUIDs or timestamps; the test also asserts the server host name is absent from everything stored. The guarantee tested is unchanged; no application code changed | `apps/notifications/tests/test_outbox.py` |

### New or corrected regression tests

| Test | Covers |
| --- | --- |
| `IdempotencyTests.test_same_key_and_same_request_returns_the_original_grant` | Same key, same payload (reason whitespace normalised) |
| `IdempotencyTests.test_same_key_with_any_different_input_is_refused_and_changes_nothing` | Different expiry, reason, administrator, member, plan → conflict, generic message, original unchanged, denial audited |
| `IdempotencyTests.test_unauthorized_or_invalid_retries_are_refused_before_the_key_is_used` | Unauthorised actor, invalid key, blank reason |
| `IdempotencyTests.test_replay_of_a_cancelled_grant_is_refused`, `…_expired_grant_…_with_or_without_the_job`, `…_suspended_grant_…` | R-03 |
| `IdempotencyTests.test_audit_layer_returning_another_grant_event_undoes_the_grant` | R-01 second layer: activation rolled back |
| `ComplimentaryConcurrencyTests.test_same_key_for_different_members_concurrently_grants_once` | R-01: 5 rounds × 3 threads/connections; 1 grant, 1 event targeting it, others `operation_conflict`, no partial activation |
| `ComplimentaryConcurrencyTests.test_same_key_for_different_plans_concurrently_grants_once` | Same, different plans; no pending row left |
| `test_complimentary_console.…test_resubmitting_after_cancellation_reports_the_real_state` / `…changed_inputs_is_a_conflict` | Console feedback matches the real state |
| `test_provisioning.…unapproved_group_permissions_block_apply_and_are_never_removed`, `…elevation_through_a_mutated_group_is_refused`, `…a_refused_apply_leaves_no_partial_changes`, `…group_changed_between_preview_and_apply_is_caught_inside_the_transaction`, `…clean_apply_keeps_unrelated_permissions_and_memberships`, `…preview_issues_no_writes` | R-04 |
| `test_outbox.DeliveryFailureTests.test_transient_smtp_failure_retries_then_fails_terminally` | R-07 |

Replaced: `test_repeated_operation_returns_the_original_grant` (asserted the R-02 defect),
`test_operation_reused_for_another_member_is_refused` (folded into the different-input test),
`test_extra_permissions_are_reported_not_removed` (asserted the R-04 defect).

### Verification of the corrective batch

Run 2026-11-03/04 on the isolated `test_clipcash_db` (development database only read).

| Command | Result |
| --- | --- |
| `manage.py test apps.memberships.tests.test_complimentary --keepdb` | 43 run, OK |
| `manage.py test apps.memberships.tests.test_complimentary.ComplimentaryConcurrencyTests --keepdb`, **5 consecutive runs** (separate PostgreSQL connections per thread) | 6 run, OK in each of 5 runs (12.0–14.8 s) |
| `manage.py test apps.administration.tests.test_complimentary_console --keepdb` | 12 run, OK |
| `manage.py test apps.accounts.tests.test_provisioning --keepdb` | 11 run, OK |
| `manage.py test apps.notifications.tests.test_outbox.DeliveryFailureTests.test_transient_smtp_failure_retries_then_fails_terminally --keepdb`, **10 consecutive runs** | OK 10/10 |
| Layer isolation probe (scratchpad, not in repository): R-01 concurrency tests with the advisory lock disabled | OK — the audit-target check alone still allows one grant per key |
| Original review probes re-run (scratchpad) | Cross-member race now 1 grant / 1 event in 5/5 rounds; the three probes that encoded the defects (R-02, R-03, R-04) now fail by being refused, as intended |
| `python -W default manage.py test apps --keepdb -v 2` (full suite) | **923 run, 923 passed, 0 failures, 0 errors, 0 skipped; 287.6 s** (292 s wall) |
| `manage.py check` | No issues |
| `manage.py makemigrations --check --dry-run` | No changes detected |
| `manage.py showmigrations` | 58 applied, 0 pending (unchanged) |

Test count 909 → 923 (+14 net: complimentary service +7, console +2, provisioning +5; R-07 corrected in place).
Warnings: only the pre-existing `RemovedInDjango60Warning` (CC-022), a `ResourceWarning` from a Bunny mock test and
the expected runner-guard `UserWarning`. `git diff --name-only a87c81b` plus untracked files: no path under
`apps/{wallets,withdrawals,payments,rewards,advertising,viewing}/` and no migration.

## 15. Deferred work (unchanged scope)

Hosting and security corrections (14-B), video integration and the complete journey (14-C), Notifications,
Support, Reports, Analytics (15-A…D), account self-service (15-E), and Phases 16–18 as set out in
`docs/acceptance/15_roadmap_reconciliation.md`. No grant revocation/shortening action was added: an
administrator can end a grant with the existing **cancel** action (`memberships.manage_memberships`).
