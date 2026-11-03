# Phase 14-A — Final Acceptance Review

**Date:** 2026-11-03 · **Reviewed state:** `main` @ `a87c81b` + uncommitted Phase 14-A working tree
**Scope:** complimentary demo membership grant (service, console, CLI), staff permission provisioning, tests and
documentation, against owner decision D1 and the Phase 14-A definition of done.
**Method:** code reading of every changed file; eight additional *review probes* executed against the isolated
test database from a scratchpad module **outside the repository** (not added to the code base); fresh focused
and full-suite test runs; read-only queries on the development database.

> Independence note: this review was carried out by the same assistant that implemented Phase 14-A. It is
> therefore "independent-style", not independent. The probes were written to try to break the implementation, and
> three defects were found that the original tests did not cover.

## Decision (original review, 2026-11-03): **CONDITIONAL PASS**

> **Update after the corrective batch (2026-11-04): PASS for implementation completeness** — R-01, R-02, R-03,
> R-04 and R-07 are fixed and regression-tested, and the full suite passes (923/923). Formal owner acceptance still
> requires the human-executed manual checklist and decision D2. See §9. The findings below are kept as originally
> recorded.

The D1 policy is implemented through the existing membership architecture, authorisation is correct at every
entry point, expiry and membership integrity hold, and no financial safeguard or financial file changed. Three
defects (R-01 – R-03) and one provisioning safety gap (R-04) must be corrected, or explicitly accepted by the owner,
before Phase 14-A is accepted. None of them can move money, bypass the Super Administrator requirement, or create
access for an unauthorised person.

## 1. Findings

| ID | Severity | Finding | Evidence | Impact | Required action |
| --- | --- | --- | --- | --- | --- |
| **R-01** | **Medium (blocks PASS)** | **Concurrent reuse of one retry key for two different members creates two grants but only one `membership.complimentary.granted` audit event**, and bypasses `operation_conflict` | Probe `test_same_operation_id_two_members_concurrently`: 5/5 runs → 2 ACTIVE grants, 1 grant event, both calls returned a Subscription. Cause: grants lock **per member**, so two members run in parallel; `audit.services._insert` treats the unique-key violation as an idempotent replay and returns the *other* member's event (`apps/audit/services.py:188-200`) | The second grant keeps its `membership.subscription.created` / `.activated` (source COMPLIMENTARY) events and the reason in `note`, but loses its dedicated grant record (reason, expiry). Violates "the audit record must not be silently lost". Requires a Super Administrator reusing an id concurrently (CLI `--operation-id`, or a forged/replayed console POST to two members) | Serialise on the operation id itself (e.g. `pg_advisory_xact_lock(hashtext(key))` or lock the key row before member work), **or** detect that `record_business_event` returned an event whose target is not this subscription and raise `operation_conflict`; add a multi-connection regression test |
| **R-02** | Low–Medium | **Retry key is bound only to member + plan**, not to actor, expiry or reason; reuse with different inputs silently returns the original grant | `apps/memberships/services.py:490-499` (`_replayed_grant`); probe → same pk, original expiry kept, no error; existing test `test_repeated_operation_returns_the_original_grant` asserts this behaviour | No duplicate grant and no change, but the caller's new inputs are ignored without warning (console then reports the original expiry). The acceptance requirement says reuse with different inputs must fail | Compare actor id, expiry and reason (all stored in the event) and raise `operation_conflict` on any mismatch; update the test |
| **R-03** | Low | **Replay after the grant was cancelled returns the CANCELLED subscription**, and the console would show "Complimentary … membership granted until …" | Probe `test_replay_after_cancellation` → returned status CANCELLED, access False; success message in `ComplimentaryGrantView.post` | Misleading success message; no access is created | On replay, return the original only if still ACTIVE, otherwise raise a clear error ("this grant was already made and has since ended"); or have the view show the actual status |
| **R-04** | Medium (provisioning) | **`provision_staff_permissions --apply` puts accounts into their role group even if that group carries extra, non-role permissions**, which elevates them | Probe `test_apply_can_extend_extra_group_permissions_to_members`: Members group given `wallets.view_wallets`, member outside the group → after `--apply` the member holds it. The preview flags such groups (`!`/`·`) but `--apply` proceeds | Elevation path if a superuser had added permissions to a role group. Not triggered today (preview on the dev database: no extra group permissions) | Refuse `--apply` (or skip that group's membership additions) while a role group holds permissions outside `ROLE_PERMISSIONS`, at least for Members; add a test. Until fixed, **run only the preview** |
| R-05 | Info | Re-adding a Content Manager to "Content Managers" restores role-defined video permissions even if a superuser removed the account from the group deliberately | Command design; `User.save` already syncs role groups on role change (`apps/accounts/models.py:275-279`) | Consistent with the role model, but should be stated | Document; keep preview-first |
| R-06 | Info | Permission/group edits are not recorded in the ClipCash audit trail | `UserAdmin.save_model` logs role/status only | Relevant to D2 Option B | See D2 brief |
| R-07 | Low (pre-existing, outside 14-A) | **Flaky test:** `test_outbox.DeliveryFailureTests.test_transient_smtp_failure_retries_then_fails_terminally` asserts that the string `"abc"` (from a simulated SMTP error) is absent from *all* stored rows, but random UUIDs and request ids are hex and sometimes contain `abc` | Failed in the review's full run because audit event id `0fdeb8c2-…-abc0db12c5f4` contained it; passed 3/3 re-runs; file last changed in Phase 02D (`30dc930`), `apps/notifications` unchanged by 14-A | Intermittent red builds (a few percent of runs); no product defect | Use a distinctive marker that cannot occur in hex (e.g. `"smtp-secret-detail"`) — a test-only change requiring separate approval |

## 2. Authorisation and idempotency

| Check | Result | Evidence |
| --- | --- | --- |
| Only an **active Super Administrator** can grant | ✅ | `grant_complimentary` → `is_super_admin` (`is_superuser` and role SUPER_ADMIN and active); tests `AuthorizationTests.test_everyone_else_is_refused_by_the_service` (content manager, staff with `memberships.manage_memberships`, finance-, support- and auditor-type permission sets, member, the member themself, suspended super admin, anonymous) |
| Both flags required | ✅ | `complimentary_grants_available()` = grants flag **and** `RESTRICTED_DEMO_MODE`; test `test_refused_unless_both_the_flag_and_restricted_demo_are_on`; console 403 and hidden button (`test_hidden_when_grants_are_not_enabled`) |
| No staff permission can bypass | ✅ | No permission is consulted; superuser status cannot be self-granted (superuser-only privilege fields; role/flags DB constraint) |
| Service rejects independently of interface | ✅ | Checks at the top of the service; CLI test with a Content Manager actor refused |
| Retry key bound to actor, member, plan, expiry, operation | ⚠️ **Partial** | Member + plan only → **R-02** |
| Reuse with different inputs fails safely | ⚠️ **Partial** | Different member/plan sequentially → `operation_conflict` ✅; different expiry/reason/actor → silent original (R-02); different member **concurrently** → second grant, lost audit event (**R-01**) |
| Duplicate form submissions | ✅ | Same form token → same subscription (console test `test_resubmitting_the_same_form_does_not_grant_twice`); new token → `membership_exists` |
| PostgreSQL concurrency | ✅ for same member; ⚠️ R-01 for cross-member key reuse | `ComplimentaryConcurrencyTests` (4 tests, real threads/connections): 5 parallel grants → 1; same op ×4 → 1; grant vs verified payment → 1 active; grant vs plan choice → consistent |
| No information in denial responses | ✅ | Unauthorised staff get the generic 403 before the member is loaded; probe: body contains neither member email nor plan; members/anonymous redirected to admin login |

## 3. Expiry and membership integrity

| Check | Result | Evidence |
| --- | --- | --- |
| Cannot exceed plan duration | ✅ | `_grant_expiry`: `expires_at ≤ now + plan.duration_days`; test "beyond plan" |
| Timezone-aware | ✅ | Naive refused (service and CLI); `+03:00` input stored as the same instant (`test_timezone_offsets_are_preserved`); console parses in the site zone (Africa/Dar_es_Salaam) and shows it |
| Eligibility ends at the boundary regardless of scheduler | ✅ | `check_access(now=expiry-1s)` allowed, `check_access(now=expiry)` denied while status still ACTIVE (`test_access_ends_exactly_at_expiry_without_the_job`) |
| Paid, suspended, scheduled memberships preserved | ✅ | Tests for active paid and suspended; probe for a queued renewal → `membership_exists`, current and queued untouched |
| Cancelled grant not eligible via stale status | ✅ | Probe: after `services.cancel` → `check_access` reason `no_membership` (access query requires ACTIVE/SUSPENDED) |
| Cannot be converted to paid by ordinary updates | ✅ | DB refuses changing amount, reference, source or expiry after activation (`test_database_refuses_a_complimentary_row_that_claims_money`); `SubscriptionAdmin` is read-only |

## 4. Financial invariants

| Check | Result | Evidence |
| --- | --- | --- |
| Changed files vs Phase 13 baseline | ✅ No file under `apps/{wallets,withdrawals,payments,rewards,advertising,viewing}/` and no migration changed or added | `git diff --name-only a87c81b` + untracked list |
| No payment or ledger entries | ✅ | `FinancialInvariantTests` snapshot of 13 tables, all ledger balances, funding statuses — identical after grant |
| Wallet balances zero / withdrawals blocked / funding disabled | ✅ | Same test: balances (0,0,0); `withdrawals_locked`; funded campaigns 0 |
| 13 lock triggers + 2 constraints | ✅ | Test DB (test) and **development DB (read-only query): 13 of 13 triggers enabled, 2 of 2 constraints present** |
| No production writes | ✅ | No production database exists/was accessed. Development DB: only reads (provisioning preview, counts, `get_site_configuration` reads); 0 complimentary subscriptions and 0 Phase 14-A audit events present |

All 15 invariants remain supported (detail: `phase_14a_complimentary_membership.md` §10).

## 5. Staff permission provisioning

| Question | Answer |
| --- | --- |
| Roles and permissions managed | Exactly `ROLE_PERMISSIONS`: Super Administrators (none — superusers), Content Managers (`videos.manage_videos`, `videos.publish_videos`, `videos.manage_categories`), Members (none) |
| Preview genuinely read-only? | **Yes** — probe captured all SQL during a preview run: 0 INSERT/UPDATE/DELETE; no audit event |
| `--apply` idempotent? | **Yes** — second run adds nothing (`test_apply_restores_role_permissions_and_groups_and_is_idempotent`) |
| Unrelated permissions unchanged? | **Yes for removal** — never removes group permissions or individual permissions; extras reported |
| Can it elevate existing users? | **Yes, in one case (R-04)**: adding an account to its role group grants any extra permissions a superuser placed on that group. It never changes roles, staff/superuser flags or status, and never creates accounts |
| Group memberships or permission definitions? | **Both**: adds missing role permissions to role groups, adds accounts to their own role group, removes other-role group memberships only with `--remove-stale` |
| Workflows lacking a second eligible actor (dev DB, preview 2026-11-03) | **All**: reward policy approval, sandbox refunds and disputes, wallet restriction lifts, demo withdrawals (2 people; 3 for high-risk). Every separation-of-duties permission has **1** holder; the dev DB has 1 account. See D2 brief |

The command was **not** run with `--apply` against the development database; no account was provisioned or elevated.

## 6. Manual acceptance readiness

The implementation document's checklist (§12) was checked against the required scenarios:

| Scenario | Covered | Gap |
| --- | --- | --- |
| Authorised grant | ✅ steps 1, 3, 5–7 | — |
| Unauthorised attempt | ✅ step 2 (staff) | Add: a member/anonymous user requesting the URL (expect admin login) |
| Grant expiry | ✅ step 14 | Needs a second member (one member already holds a grant) |
| Duplicate submission | ✅ step 8 | Add: double-click on "Grant"; two tabs (second refused) |
| Existing active membership | ✅ step 9 (existing complimentary) | A *paid* membership cannot be created manually (payments locked); covered by automated tests only |
| Complimentary display | ✅ steps 7, 11 | — |
| Member eligibility after activation | ⚠️ step 11 is general | Playback/sponsored viewing needs a playable video (Phase 14-C, CC-002); until then verify via library access messages |
| Financial non-movement | ⚠️ steps 12–13 | Add: Django admin → ledger journal entries and withdrawal requests unchanged; `/manage/health/` still says payments disabled |
| Prerequisites | ⚠️ | Dev DB has only the Super Administrator: the checklist should say to create an ACTIVE member with confirmed email and an ACTIVE plan first |

**Browser-level scenarios still requiring human execution:** `datetime-local` rendering and entry in the
browsers used for the demo; correct time-zone label and the stored expiry matching what was typed; keyboard-only
use and screen-reader announcement of the warning, errors and confirmation checkbox; layout at 360 px; Back-button
and double-click resubmission; session/CSRF expiry between review and confirm; member dashboard and *My membership*
labels; and (after 14-C) actual sponsored playback ending at the expiry.

## 7. Verification executed for this review

| Command | Result |
| --- | --- |
| `manage.py check` | No issues |
| `manage.py makemigrations --check --dry-run` | No changes detected |
| `manage.py showmigrations` | 58 applied, 0 pending |
| Focused: `manage.py test apps.memberships.tests.test_complimentary apps.administration.tests.test_complimentary_console apps.accounts.tests.test_provisioning --keepdb` | **52 run, OK** (18.3 s) |
| Review probes (scratchpad module, test DB) | 8 run, OK; outputs recorded in §1–§5 (R-01–R-04 reproduced) |
| Full suite: `python -W default manage.py test apps --keepdb -v 2` | **909 run: 908 passed, 1 failed, 0 errors, 0 skipped; 323.6 s.** The failure is `apps.notifications.tests.test_outbox.DeliveryFailureTests.test_transient_smtp_failure_retries_then_fails_terminally` — a pre-existing probabilistic false positive (R-07), not a Phase 14-A regression; it passed in 3 of 3 isolated re-runs. (The implementation run earlier the same day was 909/909.) |
| Development DB (read-only) | 13/13 lock triggers enabled, 2/2 constraints, 0 Phase 14-A rows |

## 8. Outstanding acceptance actions

1. **Fix R-01** (cross-member retry-key race) with a PostgreSQL multi-connection regression test.
2. **Fix R-02** (bind retry key to actor, expiry and reason) and update the existing replay test.
3. **Fix R-03** (replay of an ended grant) or accept the wording risk explicitly.
4. **Fix R-04** (provisioning must not add accounts to a role group carrying extra permissions); until then use
   preview only.
5. Extend the manual checklist with the gaps in §6 and execute the browser scenarios.
6. Owner decision **D2** (see `docs/decisions/D2_staff_roles_and_separation_of_duties.md`) before demonstrating any
   two-person workflow.
7. Re-run the focused and full PostgreSQL suites after the fixes.

Items 1–4 are small, contained in `apps/memberships/services.py`, `apps/accounts/management/commands/
provision_staff_permissions.py` and their tests, need no migration, and touch no financial code. They were **not**
implemented in this review, as instructed.

## 9. Corrective batch resolution (2026-11-04)

Implemented under the owner's authorisation for R-01–R-04 and R-07; details in
`phase_14a_complimentary_membership.md` §14.

| Finding | Status | Fix | Evidence |
| --- | --- | --- | --- |
| R-01 | **Fixed** | Transaction-level PostgreSQL advisory lock on the retry key before the member lock; grant rolls back unless its own audit event was stored | `ComplimentaryConcurrencyTests.test_same_key_for_different_members_concurrently_grants_once` / `…different_plans…` (5 rounds × 3 connections, 5 repeated runs); layer-isolation probe with the lock disabled; original probe now 1 grant / 1 event in 5/5 rounds |
| R-02 | **Fixed** | Key bound to administrator, member, plan, expiry instant, normalised reason and operation type; mismatch → generic `operation_conflict` | `IdempotencyTests.test_same_key_with_any_different_input_is_refused_and_changes_nothing` and console conflict test |
| R-03 | **Fixed** | Replay returns the grant only while in force; otherwise `grant_not_active` with the real state | Cancelled, expired (with and without the job) and suspended replay tests; console test confirms no success message |
| R-04 | **Fixed** | `--apply` fails closed on any unapproved role-group permission or missing role permission, re-checked inside the transaction with role groups locked; nothing removed automatically | Six provisioning tests incl. elevation attempt, no partial changes, preview with zero writes, mutation between preview and apply |
| R-05, R-06 | Information | Unchanged (documented; R-06 feeds D2) | — |
| R-07 | **Fixed (test only)** | Deterministic marker that cannot occur in hex ids/UUIDs/timestamps, plus host-name check; same invariant | 10/10 consecutive passes |

### Verification (executed for the corrective batch)

| Command | Result |
| --- | --- |
| Full suite `python -W default manage.py test apps --keepdb -v 2` | **923 run, 923 passed, 0 failures, 0 errors, 0 skipped; 287.6 s** |
| Focused suites (complimentary 43, console 12, provisioning 11) | All OK |
| Concurrency class, 5 consecutive runs | 6/6 OK each run |
| R-07 test, 10 consecutive runs | 10/10 OK |
| `check` / `makemigrations --check --dry-run` / `showmigrations` | No issues / no changes / 58 applied, 0 pending |
| Diff scope vs `a87c81b` | No financial-app file, no migration; the only file outside Phase 14-A's earlier scope is the R-07 test |

### Remaining before formal owner acceptance

1. Execute the manual checklist (`phase_14a_complimentary_membership.md` §12), especially the browser-only
   scenarios: double-click, two tabs, Back button, session expiry between review and confirm, date/time and time-zone
   display per browser, keyboard, screen reader, 360 px layout, financial non-movement counts.
2. Owner decision D2 before any two-person workflow is demonstrated.
3. Independence: implementation, review and corrections were all performed by the same assistant; an independent
   human code review of `apps/memberships/services.py` (grant section) and the provisioning command is recommended
   before commit.
