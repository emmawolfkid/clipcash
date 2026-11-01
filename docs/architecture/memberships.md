# Memberships (Phase 03)

Status: implemented in `apps/memberships`. **Online payment is not available**:
paid memberships stay pending until a future payments module (Phase 08)
confirms a payment through the integration boundary described below. No
rewards or withdrawals exist. Membership pages appear only when the
"membership features" switch is on in Site configuration.

A membership gives access to content. It is not an investment and promises no
earnings; the plan pages say so.

## Models

| Model | Purpose |
| --- | --- |
| `MembershipPlan` | Code (fixed after creation), name, description, price (whole TZS, > 0), duration in days (1–3660), benefits (display text), entitlements (registered codes), display order, status (`ACTIVE`, `INACTIVE`, `ARCHIVED`), publicly listed, version |
| `MembershipPlanVersion` | Append-only copy of a plan's commercial terms (name, price, duration, benefits, entitlements) at each version |
| `Subscription` | One member's membership: snapshots of the chosen version's terms, status, pending deadline, period (`starts_at`, `expires_at`), activation source, verified payment reference, amount paid, renewal link, reasons and timestamps |

### Pricing and history

* Editing a plan's name, price, duration, benefits or entitlements creates a new
  `MembershipPlanVersion`; description, order and visibility do not.
* A subscription copies the version's terms when the member chooses the plan.
  Later plan edits never change it: the database rejects any change to a
  subscription's snapshot columns.
* A pending choice honours its quoted price until it lapses
  (`MEMBERSHIPS_PENDING_DAYS`, default 7). A payment must match that price exactly.

## Lifecycle

```
PENDING ──verified payment / complimentary grant──► ACTIVE ◄──reinstate── SUSPENDED
   │                                                  │  ╲                   │
   └──cancel (member, admin, unpaid timeout)──► CANCELLED ◄──admin cancel────┘
                                                      │
                                     period ends ──► EXPIRED ◄── period ends (also while suspended)
```

| Transition | Who | Notes |
| --- | --- | --- |
| choose → `PENDING` | Member (ACTIVE, verified email, feature on) | One pending choice per member; choosing another plan replaces it |
| `PENDING → ACTIVE` | Payment boundary only (or complimentary grant) | See below |
| `PENDING → CANCELLED` | Member (own), administrator, expiry job (unpaid timeout) | |
| `ACTIVE → SUSPENDED` | Super Administrator or `manage_memberships` | Reason required; access blocked; time keeps running |
| `SUSPENDED → ACTIVE` | Same | Reinstated for the remaining period; if it has ended, it expires instead |
| `ACTIVE/SUSPENDED → CANCELLED` | Same | **Immediate**: access ends now. No automatic refund |
| `ACTIVE/SUSPENDED → EXPIRED` | Expiry job | When the period ends |
| `EXPIRED`, `CANCELLED` | Terminal | |

PostgreSQL enforces this state machine, creation as `PENDING` only, one-time
activation data, immutable snapshots and no deletion (migration 0003), so
direct SQL cannot skip it.

**No overlapping periods:** an exclusion constraint (`btree_gist`) guarantees a
member never has two active or suspended periods that overlap, even under
concurrent renewals.

## Payment integration boundary

`activate_with_verified_payment(subscription_id, VerifiedPayment)` is the
**only** way to activate a paid membership. It requires all of:

1. the `live_payments` feature, which is **locked off in code**;
2. a `VerifiedPayment` naming a verifier registered with
   `register_payment_verifier()`; **none exists in this phase**;
3. the verifier independently re-confirming the payment;
4. amount and currency equal to the subscription's quoted price;
5. a payment reference never used before (also a unique index);
6. a still-pending, unexpired choice for an active plan and an eligible member.

Rejections are audited (`membership.activation.rejected`). There is no admin
"mark as paid" action, and the database refuses a `PAYMENT` activation whose
amount differs from the price or lacks a reference.

**Phase 08 will:** verify provider webhooks (signature and/or provider API
query), register a verifier, call this function idempotently with the provider
transaction id as the reference, and post `membership.payment_received` to the
ledger in the same flow.

## Complimentary grants

A separate, explicit policy, not a payment workaround: a Super Administrator
grants a membership with source `COMPLIMENTARY` and amount paid 0 (the list
price is kept for the record), audited. **Disabled** unless
`DJANGO_MEMBERSHIPS_COMPLIMENTARY_GRANTS=True`. Available through
`python manage.py grant_complimentary_membership`. A short test period
(`--dev-period-minutes`) is accepted only when `DEBUG=True`.

## Renewal and plan changes

* A renewal or plan change chosen during an active period starts when the
  current period ends (no proration, no refunds).
* Renewal opens `MEMBERSHIPS_RENEWAL_WINDOW_DAYS` (14) before expiry; at most one
  renewal can be queued; suspended members cannot renew.
* There is **no automatic renewal**: every period needs its own verified payment.

## Expiry job

`memberships.process_expiry` runs every 15 minutes (Phase 02D scheduler, through
cron's `run_jobs`). It expires ended periods and cancels lapsed pending choices,
claiming rows with `SKIP LOCKED`. It is idempotent and safe to run concurrently
(tested); each change is audited once. Access never depends on the job having
run.

## Video access integration (Phases 04 and 06)

```python
from apps.memberships.access import check_access, membership_required

decision = check_access(request.user, entitlement="videos.standard")
if decision:                      # decision.reason == "ok"
    ...                           # decision.subscription is the covering membership

@membership_required("videos.premium")   # 403 otherwise
def play(request, ...): ...
```

Reasons: `feature_disabled`, `not_authenticated`, `account_inactive`,
`email_unverified`, `no_membership`, `membership_suspended`,
`entitlement_missing`, `ok`. Every call queries the database: a period that has
ended denies access immediately, and a queued renewal does not grant access
early. Entitlements are registered in `apps/memberships/entitlements.py`.

## Permissions

| Capability | Member | Content Manager | `view_memberships` | `manage_memberships` | Super Administrator |
| --- | :---: | :---: | :---: | :---: | :---: |
| Browse public plans | ✔ (also visitors) | ✔ | ✔ | ✔ | ✔ |
| Choose a plan / see own memberships | ✔ (own only) | own only | own only | own only | own only |
| View all subscriptions (admin) | ✘ | ✘ | ✔ | ✔ | ✔ |
| Suspend, reinstate, cancel | ✘ | ✘ | ✘ | ✔ | ✔ |
| Create, edit, deactivate, archive plans | ✘ | ✘ | ✘ | ✘ | ✔ |
| Complimentary grants | ✘ | ✘ | ✘ | ✘ | ✔ (when enabled) |
| Mark a membership paid | nobody | | | | |

Checks are server-side in the services and admin, not only in templates.

## Audit

Business events (same transaction as the change): `membership.plan.created`,
`.updated`, `.status_changed`; `membership.subscription.created`, `.activated`,
`.suspended`, `.reinstated`, `.cancelled`, `.expired`. Security events:
`membership.action.denied` (any refused operation) and
`membership.activation.rejected`. No payment credentials are recorded.

## Known limitations

* No payment collection: paid memberships cannot become active until Phase 08.
* No proration, refunds or automatic renewal; refunds will need the payments
  module and a refund policy.
* No expiry reminder emails yet (the outbox supports adding them).
* Member pages are English; strings are ready for Swahili translation.
* Videos do not exist yet; Phase 04 calls `check_access()` for playback.
