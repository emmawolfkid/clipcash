# Real TZS rewards and campaign funding (FIN-01)

Status: implemented, **switched off by default**. App: `apps.earnings`. Money moves only through the
double-entry ledger (`apps.wallets.services`); wallet balances are always the ledger projection.

## Flow

```
deposit recorded ──(different staff verifies receipt)──▶ advertiser verified funds
        └─ CASH → ADVERTISER_FUNDING_PENDING → ADVERTISER_FUNDING_VERIFIED   (2 postings, one transaction)
verified funds ──commit──▶ CAMPAIGN_RESTRICTED_FUNDING (campaign marked VERIFIED, entry kept as evidence)
TZS policy approved (four-eyes) ──▶ member starts a session ──▶ SessionReward RESERVED (capacity held)
verified view ──▶ reward.pending: campaign funding → member PENDING
clearance period ends (job) ──▶ reward.cleared: PENDING → AVAILABLE
fraud / revocation ──▶ reward.pending_reversed or reward.cleared_reversed: back to campaign funding
```

Credits (CR-01, `apps.rewards.DemoReward`) are a separate, non-cash path. A campaign has one approved
policy, of one type (`RewardPolicy.kind`: `CREDITS` or `TZS`), so one viewing event earns one kind or
nothing. Credits are never read, converted or posted by `apps.earnings`.

## Activation

`real_money_rewards` is on only when **both** environment settings are true and the deployment is not a
restricted demonstration:

| Setting | Meaning |
|---|---|
| `DJANGO_REAL_MONEY_REWARDS` | The switch. |
| `DJANGO_REAL_MONEY_REWARDS_APPROVED` | Recorded operational, accounting, legal and regulatory approval. |

There is no admin or database toggle. `check --deploy` refuses it with restricted demo mode (core.E009)
or sandbox payments (core.E010), and warns while it is active (core.W011). `live_payments` and
`withdrawals` remain locked in code (core.E008). While switched off: staff can prepare rates, limits and
record deposits; members see no TZS offer; nothing is posted (verification, commitment and reversal
refuse with a clear message).

## Rules

* **Offer and reservation.** At session start, inside the member row lock, the campaign's funding
  account is row-locked; the offer is accepted only if `funding − open reservations ≥ amount` and the
  member's daily limits have room. The `SessionReward` (amount fixed, database-immutable) holds that
  capacity until the session is settled. Otherwise the start is refused with the reason.
* **Daily limits** count reserved, pending, available and recovery-required rewards on the member's
  local day in the site time zone (Africa/Dar_es_Salaam). Platform and plan limits both apply: the lower
  one wins, so a plan can never exceed a platform limit. Empty = no limit.
* **Settlement** (`settle`, on commit after the session closes; job `earnings.settle_rewards` as safety
  net): verified → `reward.pending` once (idempotency key per reward, one reward per session); under
  review → stays reserved; anything else → `RELEASED` with a reason code shown to the member.
* **Clearance** (`earnings.clear_rewards`, every 5 min): pending rewards past `clears_at` (period set by
  staff, default 24 h, stored per reward) move to available, unless on hold, under review, no longer
  verified, or the wallet is restricted.
* **Reversals** return money to the campaign. After clearance, if the available balance no longer covers
  the amount the reward becomes `RECOVERY_REQUIRED`, the wallet is restricted and nothing goes negative or
  touches other money. `earnings.reconcile` (hourly) retries deferred reversals and compares reward
  records, wallet transactions and ledger balances.
* **Funding release** returns only unused, unreserved funds to the organization.
* **Duplicate references** are refused per channel (Airtel Lipa, bank) while a deposit is open or
  verified, across membership payments too; a rejected deposit frees its reference.

## Permissions (four-eyes)

| Permission | Who |
|---|---|
| `earnings.record_deposits` | Records deposits (never verifies their own: database check). |
| `earnings.verify_deposits` | Verifies the receipt in the Airtel Lipa records or bank statement. |
| `earnings.allocate_funding` | Commits / releases campaign funding. |
| `rewards.manage_reward_policies` / `rewards.approve_reward_policies` | Drafts / approves TZS rates (approver ≠ author). |
| `earnings.manage_earning_rules` | Daily limits and the clearance period. |
| `earnings.manage_real_rewards` | Holds, lifts holds, reverses rewards. |
| `earnings.view_earnings` | Read-only console access. |

## Database guards (PostgreSQL and MariaDB)

* `wallets.0006`: the Phase 09 zero-money trigger now accepts lines on a member's AVAILABLE/PENDING
  accounts **only** for the four `reward.*` posting types; any line on the RESERVED (withdrawal) account
  is refused. `WalletTransaction` rows can be inserted only as an exact mirror of a reward posting line;
  updates and deletes are refused.
* `advertising.0004`: `adv_campaign_funding_locked` is replaced by `adv_campaign_funding_evidence`
  (VERIFIED only with `funding_entry`), and a trigger checks that entry is this campaign's
  `campaign.funding_committed` posting; funding status never goes back.
* `rewards.0005`: `RewardPolicy.kind`, immutable like the other policy terms.
* `earnings.0002`: rewards are created RESERVED for exactly their TZS policy's amount; identity, amounts
  and ledger links are immutable; status transitions are fixed; deposits are created RECORDED and
  decisions are final; verified deposits and funding movements must point at matching postings; nothing
  is deleted.

## Migrations and rollback

Forward migrations add tables, columns and guards only; existing data is unchanged (existing campaigns
stay UNFUNDED, existing policies become `CREDITS`, existing credits stay non-cash).

Rollback **before** any real posting: reverse to `wallets 0005`, `advertising 0003`, `rewards 0004`,
`earnings zero` — the previous guards are restored exactly.

Rollback **after** real postings exist is not a data operation: posted ledger entries, wallet
transactions and reward records are immutable and must remain (they are financial records). Reversing
`advertising.0004` fails while any campaign is funded (by design: the restored constraint forbids it).
To stop issuing, switch `DJANGO_REAL_MONEY_REWARDS` off instead; obligations already created remain owed
and visible.

## Jobs and hosting (NetPoa)

All three jobs run from the existing cron-driven runner (`manage.py run_jobs` every minute; no persistent
worker). They are bounded, idempotent and safe to overlap. If cron runs only every 5 minutes, settlement
falls back to that latency for sessions not settled on commit, and clearance can be up to one run late.

## Not in this phase

Withdrawals and payouts (FIN-02), live provider integrations, automatic deposit matching. Production
activation also needs funding, accounting, legal and regulatory sign-off (see `ledger.md`, "Assumptions
requiring professional review").
