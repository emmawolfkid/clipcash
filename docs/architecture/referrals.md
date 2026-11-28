# Referral programme (REF-01)

A single-level referral programme. A member shares a personal code or link; a new member who signs up
with it and then completes **genuine, server-verified activity** can earn the referrer a reward, paid
only from a **separately verified ClipCash marketing budget** into the referrer's existing wallet.

Code: `apps/referrals` (`services.py`, `funding.py`, `console.py`, `views.py`), database guards in
`apps/referrals/migrations/0002_integrity.py`, ledger accounts and wallet rules in
`apps/wallets/migrations/0008_referral_accounts.py`.

## What the programme is not

* No multi-level commissions, downline rewards or recruitment chains: only the direct referrer can ever be
  rewarded.
* No reward for registering or buying a membership. Membership can be an eligibility condition; membership
  fees can never fund rewards (database funding-separation rules).
* No guaranteed income and no unfunded rewards: every reward is allocated from committed budget under a row
  lock; when the budget is used up, referrals qualify without a reward (and are retried for 30 days).

## Lifecycle

| Status | Meaning |
|---|---|
| `AWAITING` | Registered with a code; waiting for the new member's verified activity |
| `UNDER_REVIEW` | Qualified with risk signals; a reviewer decides before any reward |
| `QUALIFIED` | Qualified, no reward allocated; `outcome_reason` explains why (rewards off, programme closed, limit, budget, membership) |
| `REWARD_PENDING` | Reward posted to the referrer's pending balance |
| `REWARD_CLEARED` | Reward moved to the available balance (withdrawable under FIN-02) |
| `REJECTED` | A reviewer rejected it |
| `REVERSED` | The reward was reversed (staff, or qualifying activity withdrawn) |
| `EXPIRED` | Did not qualify within the qualification period |

Transitions are enforced by database triggers. Qualification is evaluated when one of the new member's
viewing sessions closes or is reversed (after commit) and by the `referrals.evaluate` job.

## Qualification rules and versioning

Configurable by staff: programme availability (tracking), rewards on/off and amount, referrer and new-member
membership conditions, minimum account age, verified sponsored views required, different days required,
qualification period, daily and lifetime rewards per referrer, clearance period.

* Every change is a new `ReferralPolicy` version, **proposed** by one person
  (`referrals.manage_referral_policy`) and **approved** by a different person
  (`referrals.approve_referral_policy`); a CHECK constraint enforces the four-eyes rule and triggers make
  approved versions final.
* **Existing referrals:** each referral keeps the version in force when the new member registered (its
  qualification rules and its maximum reward). At allocation the **lower** of that amount and the current
  version's amount applies, and the current version must still offer rewards. A rule change can therefore
  lower or stop an unpaid reward but never raise it; allocated rewards never change. A referral registered
  while no reward was offered never earns one later (no retroactive claims). Daily and lifetime limits use
  the stricter of the two versions; the clearance period the longer.
* Counted activity: the new member's `VERIFIED_EVIDENCE` sessions finalized inside the qualification period,
  excluding sessions under review or rejected; revoked views stop counting.

## Funding and ledger

New chart accounts (platform scope, overdraft-protected):

| Account | No. | Funding class | Purpose |
|---|---|---|---|
| `MARKETING_FUNDING_PENDING` | 2140 | advertiser-unverified | Marketing money received, not yet verified |
| `MARKETING_FUNDING_VERIFIED` | 2150 | advertiser-verified | Verified marketing budget, not committed |
| `REFERRAL_PROGRAMME_FUNDING` | 2210 | reward-restricted | Committed referral budget: the only source of referral rewards |

The marketing accounts reuse the existing funding classes so the core ledger trigger
(`ledger_entry_finalize_check`) and its funding-separation rules apply **unchanged**: membership money can
never share an entry with them, and reward-restricted money can only come from verified money.

Posting examples (whole TZS):

| Step | Posting type | Debit | Credit | Feature |
|---|---|---|---|---|
| Marketing deposit verified (1st entry) | `marketing.deposit_received` | CASH 10,000 | MARKETING_FUNDING_PENDING 10,000 | referral_rewards |
| (2nd entry, same transaction, different person) | `marketing.deposit_verified` | MARKETING_FUNDING_PENDING 10,000 | MARKETING_FUNDING_VERIFIED 10,000 | referral_rewards |
| Budget committed | `referral.funding_committed` | MARKETING_FUNDING_VERIFIED 5,000 | REFERRAL_PROGRAMME_FUNDING 5,000 | referral_rewards |
| Referral qualifies | `referral.reward_pending` | REFERRAL_PROGRAMME_FUNDING 1,000 | MEMBER_REWARD_PENDING (referrer) 1,000 | referral_rewards |
| Clearance | `referral.reward_cleared` | MEMBER_REWARD_PENDING 1,000 | REWARD_LIABILITY (available) 1,000 | referral_settlement |
| Reversal (pending) | `referral.pending_reversed` | MEMBER_REWARD_PENDING 1,000 | REFERRAL_PROGRAMME_FUNDING 1,000 | referral_settlement |
| Reversal (cleared) | `referral.cleared_reversed` | REWARD_LIABILITY 1,000 | REFERRAL_PROGRAMME_FUNDING 1,000 | referral_settlement |
| Unused budget released | `referral.funding_released` | REFERRAL_PROGRAMME_FUNDING | MARKETING_FUNDING_VERIFIED | referral_settlement |

Withdrawals then follow FIN-02 unchanged (`withdrawal.reserved` / `withdrawal.paid_manual`). Wallet history
keeps referral money separate (`REFERRAL_CREDIT`, `REFERRAL_CLEARANCE`, `REFERRAL_REVERSAL` categories;
`referral_reward` source type). A marketing transaction reference can never also back a campaign deposit or a
membership payment.

If a cleared reward is reversed after the money was withdrawn, the reward becomes `RECOVERY_REQUIRED` and the
wallet is restricted (fraud review); nothing goes negative and no other money is taken.

## Fraud controls

* Database: exactly one referrer per referred member (one-to-one); no self-referral (CHECK); no circular pair;
  a referral can only be inserted within an hour of the referred account's creation and with the referrer's
  own code; attribution, policies, rewards, deposits and budget movements are immutable and undeletable; a
  reward can never exceed its policy amount and must match its ledger entry.
* Services: row locks on the referral, the referrer's code row (limits) and the programme account (budget);
  idempotency keys per referral; staff can never enter an amount (approval applies the normal rules);
  reviewers can never decide a referral they are part of.
* Risk signals (send to review, never auto-reject): shared network address, shared address and browser, shared
  payout destination fingerprint, matching identity-check details, more than 5 referrals by one referrer in 24
  hours, flagged or rejected qualifying activity, restricted referrer wallet. Legitimate members may share
  networks, devices or family payout numbers.
* Phone numbers and identifiers are unique per account, so the same number cannot register twice.

## Activation

| Feature | Settings | Allows |
|---|---|---|
| (none) | policy `tracking_open` | Codes, links, attribution, qualification (no money) |
| `referral_rewards` | `DJANGO_REFERRAL_REWARDS` + `DJANGO_REFERRAL_REWARDS_APPROVED` | New rewards, marketing deposit verification, budget commitment |
| `referral_settlement` | `DJANGO_REFERRAL_REWARDS_APPROVED` | Clearing and reversing existing rewards, releasing budget |

Both are off by default and in every restricted demonstration (`check --deploy` core.E015/E016, warning
core.W017). FIN-01 and FIN-02 switches are independent and unchanged.

## Permissions

`referrals.view_referrals`, `manage_referral_policy`, `approve_referral_policy`, `review_referrals`,
`record_marketing_deposits`, `verify_marketing_deposits`, `allocate_referral_budget`. Minimum separation: a
policy proposer and a different approver; a deposit recorder and a different verifier.

## Jobs

`referrals.evaluate` (15 min), `referrals.clear_rewards` (5 min), `referrals.reconcile` (hourly). Reconciliation
checks the programme and marketing balances against deposits, commitments and rewards, wallet-transaction
mirrors and status agreement; FIN-01's wallet reconciliation now includes referral rewards.

## PostgreSQL

The PostgreSQL triggers in `referrals 0002` and `wallets 0008` mirror the MariaDB ones but are **not tested
locally** (no PostgreSQL server here). The retroactive-attribution check uses `now()` on PostgreSQL and
`UTC_TIMESTAMP()` on MariaDB against `accounts_user.date_joined` (stored in UTC by Django).
