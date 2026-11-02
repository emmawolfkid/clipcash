# Demo rewards (Phase 07)

A rewards engine running in **demonstration mode**. It records what a verified
view *would* earn under an approved policy.

**Demo rewards are not money.** They aren't balances, obligations or payable
amounts, they can't be withdrawn or spent, and they're never posted to the
ledger. The rewards app doesn't import `apps.wallets` (a test checks this).
There is no field or flag that turns a demo reward into a real one. Real
rewards need reconciled advertiser funding (Phase 08), the locked
`real_money_rewards` flag, and a separate, reviewed implementation.

## Data flow

```
Phase 06 verified session (FINALIZED, VERIFIED_EVIDENCE, slot)
   → rewards.evaluate_pending job (every 60 s) / console "Evaluate now"
      → reward_eligibility(session)            # server data only
      → lock session → lock CampaignRewardBudget
      → one DemoReward per session: DEMO_QUALIFIED (amount reserved) or DEMO_REJECTED
Staff revoke a verified view (Phase 06 console)
   → viewing.session_reversed signal, inside the same transaction
      → DemoReward DEMO_REVERSED, budget released exactly once
rewards.reconcile_reversals job (every 15 min) → backstop for any missed reversal
```

## Policies (`RewardPolicy`)

- **Scope:** a policy belongs to one campaign **and one approved campaign revision**, with an increasing `version` number.
- **Amount:** a whole-TZS amount, greater than 0 and at most the campaign's approved `demo_reward_per_view`. Floats and fractions are rejected.
- **Statuses:** DRAFT → APPROVED or REJECTED; APPROVED → RETIRED.
- **Four-eyes approval:** the approver must be a different person from the author. This is enforced by both the service and a database check.
- **One approved policy per campaign at a time:** approving version 2 retires version 1 at the same instant.
- **Immutable once decided:** the campaign, revision, version, amount, author and decision never change. To change the amount, create a new version.
- **Effect on past rewards:** none. Each reward stores the policy and version it was recorded under, and past rewards are never re-priced.
- **Campaign changes:** a changed campaign revision invalidates drafts for the old revision (`campaign_revision_changed`), and needs a new policy approval. A policy change itself doesn't need the campaign to be re-approved, because the amount is capped by what the campaign review already approved.

Permissions are `rewards.manage_reward_policies`, `rewards.approve_reward_policies`
and `rewards.manage_demo_rewards`. Super Administrators hold all three; Content
Managers hold none.

## Eligibility (`services.reward_eligibility`)

Eligibility is **judged at the moment of the verified view**. The policy used
is the one that was in force when the session was finalized, for the campaign
revision the member watched.

- A campaign that later **completes normally** keeps its rewards.
- Fraud reversals, staff revocations and **campaign cancellation** disqualify a view.

**Permanent reasons** record a `DEMO_REJECTED` decision:

| Reason | Meaning |
| --- | --- |
| `session_not_verified` | The view wasn't verified. |
| `session_not_finalized` | The session wasn't finished. |
| `slot_missing` | The view wasn't counted. |
| `campaign_cancelled` | The campaign was cancelled. |
| `no_policy` | No approved policy was in force at the time of the view. |
| `revision_mismatch` | The policy is for a different campaign revision. |
| `policy_amount_exceeds_campaign` | The policy amount exceeds what the campaign approved. |
| `budget_exhausted` | The simulated budget was used up. |

**Retry-later reasons** record nothing; the job tries again:

| Reason | Meaning |
| --- | --- |
| `feature_disabled` | The `advertisers` flag is off. |
| `review_pending` | The view is under fraud review. |
| `organization_not_in_good_standing` | The advertiser is suspended. |
| `campaign_paused_by_staff` | Staff paused the campaign. |

Client-side progress never counts. Only Phase 06's server decision does.

## Calculation and budget

- **Amount:** `amount = policy.reward_amount`, in integer whole TZS. There is no arithmetic beyond addition and subtraction of integers.
- **`demo_allocated`:** the campaign's approved `demo_budget_amount`. Changing it needs the campaign to be re-reviewed (Phase 05).
- **Reserving capacity:**
  - `CampaignRewardBudget.demo_consumed` is reserved under a row lock: a reward qualifies only if `consumed + amount <= allocated`. Otherwise the decision is `budget_exhausted`.
  - A database trigger also refuses any consumption above the budget, and a check refuses negative consumption.
- **`demo_remaining`:** `allocated - consumed`, never shown as negative.
- **Releasing capacity:** a reversal releases the amount exactly once. It is the single locked status change DEMO_QUALIFIED → DEMO_REVERSED.
- **Labelling:** these figures are always shown as simulated, never as cash, deposits, revenue or funded money.

## Demo reward records (`DemoReward`)

| Status | Meaning |
| --- | --- |
| `DEMO_QUALIFIED` | A simulated reward is recorded, with an amount above 0 and its policy. |
| `DEMO_REJECTED` | The view wasn't eligible. Amount 0, with reason codes. |
| `DEMO_REVERSED` | Was qualified, then reversed. The record and its amount are kept, with the reversal time, actor and reason. |

There's no "pending" record: transient conditions are retried, not stored.

- **One decision per session:** a one-to-one database link enforces it.
- **The only allowed change** is QUALIFIED → REVERSED. Nothing else can change, and nothing can be deleted (database trigger).

## Reversals

Reversals reach a demo reward in three ways:

| Trigger | What happens |
| --- | --- |
| A reviewer revokes a verified view | `viewing.services.revoke_verified_session`. The view's outcome and review become REJECTED, its counted view is released, and its evidence stays frozen. This uses migration `viewing.0003`. The demo reward is reversed **in the same transaction** through the `session_reversed` signal. |
| A reviewer rejects a suspicious view | It never had a reward, because views under review wait. |
| A staff member reverses a reward directly | `reverse_reward`, which needs a reason and protects against stale decisions. |

The `rewards.reconcile_reversals` job is a backstop: it reverses any qualified
reward whose session is no longer verified. Every path is idempotent and locks
in the same order (session → reward → budget), so reversals can't deadlock or
release capacity twice.

## Interfaces

**Members, `/rewards/`:**

- per-campaign simulated totals;
- full history, including not-eligible and reversed records, with plain explanations.

The page labels everything "Simulated rewards only". It never shows balances, or wording such as "withdrawable" or "payout pending".

**Console, `/manage/rewards/`:**

- **Rewards page:** campaign totals (recorded, reversed, not eligible, and demo consumed against allocated), counts of not-eligible reasons, filters and pagination, "Evaluate verified views now", and reversal with a reason.
- **Policies page:** drafts, approval, rejection and retirement, and version history.
- **Viewing review:** a "Revoke verified view" action on verified sessions.

The Django admin is read-only.

## Audit (category `REWARDS`)

| Area | Events |
| --- | --- |
| Policies | `rewards.policy.created`, `.approved`, `.rejected`, `.retired` |
| Decisions | `rewards.decision.qualified`, `.rejected` |
| Budget | `rewards.budget.exhausted` |
| Reversals | `rewards.reward.reversed` |
| Security | `rewards.duplicate.refused`, `rewards.action.denied` |
| Viewing (Phase 06) | `viewing.session.revoked` |

Metadata holds IDs, versions, whole-TZS amounts and reason codes. It never
contains emails, tokens or playback links.

## Phase 08 boundary

- Phase 07 creates no financial obligations.
- A future real-money step must, in its own reviewed implementation:
  1. require `Campaign.financially_eligible`, meaning funding VERIFIED from reconciled advertiser money, with the database constraint `adv_campaign_funding_locked` relaxed only together with evidence of that funding;
  2. post through the ledger's `reward.allocated` type from the campaign's `CAMPAIGN_RESTRICTED_FUNDING`, never from membership income (the ledger chart already forbids mixing the two);
  3. link each real reward one-to-one to a demo-reward decision or watch session, and honour reversals.
- Demo records must never be "upgraded" in place.
