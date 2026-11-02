# Member wallets (Phase 09)

A wallet is a member's identity in the ledger. It is **not money**. In this
phase every wallet balance is TZS 0, and the database refuses anything that
could change that.

## Model

`Wallet` (in `apps.wallets`, next to the ledger) has:

- **Member:** one-to-one. A database-unique constraint allows one wallet per member.
- **Number:** a stable, random `CW-XXXXXXXXXX` that never changes.
- **Currency:** TZS, enforced by a check.
- **Status:** ACTIVE, or RESTRICTED while any restriction isn't lifted.
- **Three linked ledger accounts**, each member-scoped and one-to-one with the wallet:

| Bucket | Account type | Chart no. | Class | Funding class |
| --- | --- | --- | --- | --- |
| Available | `REWARD_LIABILITY` | 2300 | Liability (credit-normal) | REWARD_RESTRICTED |
| Pending | `MEMBER_REWARD_PENDING` (new) | 2310 | Liability | REWARD_RESTRICTED |
| Reserved | `MEMBER_WITHDRAWAL_RESERVED` (new) | 2320 | Liability | REWARD_RESTRICTED |

The two new account types are additive and overdraft-protected. They're in
the same funding class as `REWARD_LIABILITY`, so the ledger's existing
funding-separation triggers apply unchanged: membership money can never reach
them.

**Eligibility:** only a verified, ACTIVE, non-staff `MEMBER` can have a wallet;
staff accounts never get one. The member opens it with a POST, and a GET
never creates anything.

**Creation** is idempotent and safe under concurrent requests: a member-row
lock plus the unique constraint, which is tested with five parallel requests.
When a wallet is created, a database trigger checks that it links its own
member's accounts, of the right types, with zero balance. After that, the
wallet's identity can't change and it can't be deleted.

## Balances are a projection of the ledger

`wallet_services.balances(wallet)` sums `JournalLine` rows (credits − debits
for credit-normal liabilities) for the wallet's three accounts, in one query.

- It never reads an editable column.
- `LedgerAccount.debit_balance` is only cross-checked during reconciliation.
- Amounts are exact integers in TZS.
- **Only "available" is spendable**, and only when the wallet isn't restricted. Pending and reserved never are.
- Demo rewards (`apps.rewards`) and sandbox payments (`apps.payments`) are never read.

## Zero-money enforcement (layered)

1. **Database (hard lock, migration `wallets/0005`):** the trigger
   `a_wallets_block_member_postings` refuses **any journal line on a
   wallet-linked account**. `WalletTransaction` refuses every insert, update
   and delete. These hold even if feature flags or service code were changed:
   a test opens the posting gates and the database still refuses. Activation
   needs a reviewed migration that replaces these triggers; no setting can do it.
2. **Ledger gates (Phase 02C):** every posting type that could reach member
   accounts (`reward.allocated`, `withdrawal.*`) needs `real_money_rewards` or
   `withdrawals`. Those flags are locked off in code and have no settings field.
3. **Service:** `request_wallet_posting(...)` is the only future entry point. It
   always raises `WalletPostingLocked` and records `wallets.posting.refused`.
   There's no credit, debit or transfer endpoint, and no balance controls in
   the console or the Django admin.

**Remaining global boundary:** ledger accounts **not** linked to a wallet are
still governed only by layer 2. The Phase 02C tests use such synthetic
accounts to prove the accounting works. Real members' wallet accounts are
always linked, so they're covered by layer 1.

## Transactions (future categories only)

`WalletTransaction` is an immutable projection of one journal entry onto one
wallet bucket. It has:

- a unique reference and a unique idempotency key;
- a unique `(wallet, journal entry, bucket)`;
- a category: REWARD_CREDIT, WITHDRAWAL_RESERVATION, WITHDRAWAL_SETTLEMENT, WITHDRAWAL_RELEASE, CORRECTION or REVERSAL.

The table is locked in this phase, so history pages show an empty state. No
fake transactions are created.

## Restrictions

- **Placing one** needs `wallets.manage_wallet_restrictions`, a type (security hold, fraud review, compliance) and a reason. The wallet becomes RESTRICTED.
- **Lifting one is a two-person action:** one person requests it with a reason, and a **different** person approves or declines it.
  - The service checks this on the locked row.
  - A database check refuses `lifted_by = lift_requested_by`.
  - A trigger keeps the type, reason and author fixed, and LIFTED is final.

## Reconciliation

`wallet_services.reconcile()` runs:

- as the job `wallets.reconcile` (hourly);
- as the command `python manage.py reconcile_wallets`;
- from the console's "Run reconciliation now" button.

It checks:

| Finding | What it means |
| --- | --- |
| `member_not_eligible` | The wallet owner is a staff account. |
| `account_ownership` | A linked account has the wrong type, scope or owner. |
| `balance_projection_mismatch` | The journal-line sum differs from the account balance. |
| `unexpected_balance` | A wallet has a balance or any journal line in Phase 09. |
| `unexpected_transactions` | Wallet transactions exist. |
| `missing_audit` | No `wallets.wallet.created` event exists for the wallet. |
| `status_mismatch` | The wallet's status differs from its restrictions. |
| `orphan_member_account` | A real member has a wallet-type account that isn't linked to a wallet. |

Findings become `WalletDiscrepancy` rows, recorded once per open finding (and
refreshed after that). They're **never corrected automatically**. Resolving
one records a note and changes no financial record.

## Interfaces

**Members, `/wallet/`:**

- an "Open my wallet" button;
- the wallet number and the available, pending and reserved amounts (TZS 0);
- an empty transaction history;
- a restriction notice when one applies;
- a clear statement that demo rewards and sandbox payments are never wallet money.

The dashboard links to the wallet.

**Console, `/manage/wallets/`:**

- **Directory:** search by **exact** wallet number or exact email, so partial guessing finds nothing. Pages use random UUIDs.
- **Detail:** every view is recorded as `wallets.wallet.inspected`. It shows balances, account codes, restrictions (place, request lift, approve or decline), transactions, findings and audit history.
- **Reconciliation:** the findings queue and the run button.

**Permissions:** `wallets.view_wallets` and `wallets.manage_wallet_restrictions`
(Super Administrators). Content Managers get neither.

## Audit (category `WALLETS`)

| Area | Events |
| --- | --- |
| Wallets | `wallets.wallet.created`, `wallets.wallet.inspected` (security) |
| Restrictions | `wallets.restriction.placed`, `.lift_requested`, `.lifted`, `.lift_declined` |
| Security | `wallets.access.denied`, `wallets.posting.refused` |
| Reconciliation | `wallets.reconciliation.discrepancy`, `wallets.discrepancy.resolved` |

Metadata holds wallet numbers, types and reason codes, never emails or
payloads.

## Future reward credit contract (not implemented)

A future `credit_reward(session, ...)` may only be added in a reviewed phase,
together with a migration that narrowly lifts the database lock. It must:

1. take a Phase 06 session that is FINALIZED with VERIFIED_EVIDENCE and has no pending review;
2. apply an approved reward policy for that campaign revision;
3. require a campaign that is `financially_eligible`: funding VERIFIED from an authenticated, reconciled advertiser deposit (live Phase 08 successor, not sandbox), and the campaign funding lock relaxed only with that evidence;
4. post `reward.allocated` from that campaign's `CAMPAIGN_RESTRICTED_FUNDING` to the member's wallet account, as one balanced real-ledger entry with an idempotency key derived from the session, exactly once;
5. check available campaign capacity under a row lock;
6. handle fraud reversals with a ledger reversal plus a `REVERSAL` wallet transaction;
7. require explicit live-money authorization.

**Phase 07 demo rewards stay permanently separate.** They are simulated
records in `apps.rewards`, are never upgraded in place, and are never read as
balances.

## Phase 10 withdrawal contract (not implemented)

A future withdrawal flow must:

- **check ownership and eligibility:** a verified ACTIVE member, the wallet's owner, an unrestricted wallet, and the member's account status;
- **validate the available balance:** available only, read under a lock on the wallet's available account (`select_for_update`, in the ledger's fixed account-lock order);
- **reserve:** post available → `MEMBER_WITHDRAWAL_RESERVED` with a withdrawal-request idempotency key, plus a WITHDRAWAL_RESERVATION transaction;
- **settle idempotently:** post reserved → `WITHDRAWAL_CLEARING` → payout, once per provider payout reference (the existing `withdrawal.paid` single-use rule);
- **release on payout failure:** post reserved → available (WITHDRAWAL_RELEASE), once;
- **handle reversals:** use ledger reversals, never edits;
- **honour holds:** a restriction blocks new reservations, and existing reservations wait for review.

None of this exists yet. Withdrawals stay locked (`withdrawals` flag and the wallet trigger).
