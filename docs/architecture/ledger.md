# Financial ledger foundation (Phase 02C)

Status: infrastructure implemented in `apps/wallets`. **No real money moves.**
Every posting type requires a financial feature (`live_payments`,
`real_money_rewards` or `withdrawals`) that is locked off in code, so the
posting service refuses everything outside tests. There are no payment
providers, no member balances, no rewards and no withdrawals.

## Components

| Module | Purpose |
| --- | --- |
| `chart.py` | Account types: chart number, classification, funding class, scope, overdraft protection |
| `models.py` | `LedgerAccount`, `JournalEntry`, `JournalLine`; constraints generated from the chart |
| `posting_types.py` | The only accepted kinds of entry and their rules |
| `services.py` | `post_entry`, `reverse_entry`, `open_account`, `set_account_status` |
| `migrations/0002_ledger_integrity.py` | PostgreSQL triggers enforcing integrity |
| `migrations/0003_platform_accounts.py` | Platform accounts of the chart (no entries) |
| `admin.py` | Read-only views (`wallets.view_ledger`) |

## Chart of accounts

Accounting classification (where an account sits in the balance sheet or
income statement) is separate from the **funding class**, which decides what
money may finance rewards. Account names and funding classes are internal
controls: **they do not create legal escrow, trust or ring-fenced status.**
That requires contracts, bank account arrangements and regulatory approval.

| No. | Type | Classification | Funding class | Scope | Protected |
| --- | --- | --- | --- | --- | :---: |
| 1000 | `CASH`: cash at bank and mobile-money float | Asset | Treasury | Platform | |
| 1100 | `PAYMENT_CLEARING`: provider clearing | Asset | Treasury | Platform | |
| 1200 | `ADVERTISER_RECEIVABLE`: invoiced, not received | Asset | Advertiser unverified | Advertiser | |
| 2000 | `MEMBERSHIP_DEFERRED_REVENUE`: fees for undelivered periods | Liability | Membership | Platform | ✔ |
| 2100 | `ADVERTISER_FUNDING_PENDING`: received, not verified | Liability | Advertiser unverified | Advertiser | ✔ |
| 2110 | `ADVERTISER_FUNDING_VERIFIED`: verified, unallocated | Liability | Advertiser verified | Advertiser | ✔ |
| 2200 | `CAMPAIGN_RESTRICTED_FUNDING`: committed to one campaign | Liability | Reward restricted | Campaign | ✔ |
| 2300 | `REWARD_LIABILITY`: rewards owed to a member | Liability | Reward restricted | Member | ✔ |
| 2400 | `WITHDRAWAL_CLEARING`: approved withdrawals in progress | Liability | Reward restricted | Platform | ✔ |
| 2500 | `REFUNDS_PAYABLE`: approved refunds not yet paid | Liability | Operating | Platform | ✔ |
| 3000 | `OWNER_EQUITY` | Equity | Operating | Platform | |
| 4000 | `MEMBERSHIP_REVENUE`: earned fees | Revenue | Membership | Platform | |
| 4100 | `ADVERTISING_SERVICE_REVENUE`: platform advertising fees | Revenue | Operating | Platform | |
| 4900 | `OTHER_OPERATING_REVENUE` | Revenue | Operating | Platform | |
| 5000 | `PAYMENT_PROCESSING_FEES` | Expense | Operating | Platform | |
| 5900 | `OPERATING_EXPENSES` | Expense | Operating | Platform | |

"Protected" accounts may never have a negative normal-side balance. Platform
accounts are created by migration; advertiser, campaign and member accounts are
opened on demand (`open_account`) with an opaque scope id until those modules
exist. Account codes are stable: `cash`, `campaign_restricted_funding:<id>`.

## Journal model

* `JournalEntry`: posting type, description, currency (TZS), total, posting
  time, source type/id, external reference (unique per source type), related
  entry, reversal link (one-to-one), idempotency key (unique), content
  fingerprint, actor id, request id. Status is always `POSTED`: entries are
  written posted, in one transaction; there are no drafts.
* `JournalLine`: entry, line number, account, side (`DEBIT`/`CREDIT`),
  positive whole-shilling amount (`apps.core.money`), memo. One line per account
  per entry.

## Posting types and rules

| Posting type | Debit | Credit | Feature | Extra rules |
| --- | --- | --- | --- | --- |
| `membership.payment_received` | Payment clearing | Membership deferred revenue | live_payments | External reference |
| `membership.revenue_recognized` | Membership deferred revenue | Membership revenue | live_payments | |
| `membership.refund_approved` | Membership deferred revenue | Refunds payable | live_payments | |
| `advertiser.funding_received` | Payment clearing | Advertiser funding pending | live_payments | External reference |
| `advertiser.funding_verified` | Advertiser funding pending | Advertiser funding verified | live_payments | References one receipt, same amount, same advertiser, different person from the receipt's actor, once per receipt, not if reversed |
| `advertiser.refund_approved` | Advertiser funding verified | Refunds payable | live_payments | |
| `campaign.funding_committed` | Advertiser funding verified | Campaign restricted funding | real_money_rewards | |
| `campaign.funding_released` | Campaign restricted funding | Advertiser funding verified | real_money_rewards | |
| `reward.allocated` | Campaign restricted funding | Reward liability | real_money_rewards | |
| `withdrawal.approved` | Reward liability | Withdrawal clearing | withdrawals | |
| `withdrawal.paid` | Withdrawal clearing | Payment clearing | withdrawals | External reference; references one approval, same amount, once |
| `payment.settled` | Cash | Payment clearing | live_payments | External reference |
| `payment.fee_charged` | Payment processing fees | Payment clearing | live_payments | |
| `refund.paid` | Refunds payable | Payment clearing | live_payments | External reference |

Every entry must also have ≥ 2 lines, positive whole-shilling amounts, equal
debits and credits, ACTIVE TZS accounts and an authorized poster: system
processes (`actor=None`) or an ACTIVE Super Administrator.

## Funding separation

| Requirement | Enforced by | Where |
| --- | --- | --- |
| Membership fees are never campaign funding or rewards | No posting type pairs them, **and** the database rejects any entry mixing membership with advertiser/reward funds | Service + DB |
| Rewards only from verified campaign funding | Reward-restricted credits may only be balanced by debits from verified-advertiser or reward-restricted accounts | Service + DB |
| Funding verified independently before use | Verified funds may only come from pending funds; verification references one receipt, matching amount and owner, by a different person | DB + service |
| Allocations ≤ available verified campaign funding | Overdraft protection on campaign funding, checked under row lock | DB |
| Funds cannot be committed twice | Overdraft protection on verified funding; idempotency keys | DB |
| Withdrawal liabilities cannot be paid twice | One payout per approval (partial unique index), amount must match, overdraft protection on clearing and reward liability | DB + service |

**Needs later modules:** linking a campaign account to its advertiser,
checking a receipt against the provider (Phase 08), deciding reward amounts
(Phase 07), withdrawal approval workflows and limits (Phase 10), and
reconciliation. The ledger cannot know where money in a pooled bank account
came from; true provenance depends on provider references and bank
segregation.

## Reversals

* `reverse_entry(entry, reason, actor)` posts the exact mirror (same accounts and
  amounts, opposite sides), linked by `reverses`.
* At most one reversal per entry (one-to-one key), a reversal is never itself
  reversed, and the mirror is verified by the database at commit.
* A reversal is refused if it would overdraw a protected account. For example,
  campaign funding cannot be un-committed after rewards were allocated from it;
  the rewards must be reversed first.
* Reversals need a person (an ACTIVE Super Administrator), a reason and an
  idempotency key, and are disabled with their posting type's feature.
* A reversed entry cannot be used as a related entry (e.g. a reversed receipt
  cannot be verified).

## Database-level integrity guarantees (PostgreSQL)

| Guarantee | Mechanism |
| --- | --- |
| Entries balance, have ≥ 2 lines, total matches | Deferred constraint triggers (`ledger_line_balanced`, `ledger_entry_complete`) at commit; the service also forces them immediately |
| Balances cannot be faked | New accounts start at 0; `debit_balance` changes only inside the line-insert trigger |
| No overdraft of protected accounts | Check constraint on the account row, evaluated under the row lock taken by the balance update |
| Posted entries and lines immutable | `ledger_immutable` trigger rejects UPDATE/DELETE; accounts cannot be deleted; account identity columns immutable |
| Amounts, currency, status, sides, reference uniqueness, single-use relations | Check constraints and (partial) unique indexes |
| Account type ↔ classification/funding class/scope/protection | Check constraints generated from the chart |
| Funding separation and reversal mirroring | `ledger_verify_entry()` at commit |

Direct SQL that bypasses Django is rejected by the same rules (tested).

**Name resolution (RG-03).** The trigger functions name their tables without a schema. Until
migration `core.0004` they resolved those names through the caller's `search_path`, in which
PostgreSQL searches the session's temporary schema first: a session that created a temporary
`wallets_journalline` or `wallets_ledgeraccount` could make `ledger_verify_entry()` or
`ledger_line_apply()` read its own copy, so an unbalanced entry could commit and a frozen account
could be posted to (reproduced in `apps/core/tests/test_database_hardening.py`). Every function now
runs with `search_path = pg_catalog, public, pg_temp`; a test keeps that true for any function a
future migration adds, and `CREATE OR REPLACE FUNCTION` in a new migration must repeat the
`SET search_path` clause (otherwise it is dropped and the test fails).

**Limitations:** the table owner or a PostgreSQL superuser can disable
triggers, alter constraints or TRUNCATE tables. Locally `clipcash_user` owns the
tables. Production must run the web application as a role without ownership,
with only `SELECT, INSERT` on journal tables and `SELECT, INSERT, UPDATE(status,
name, updated_at)` on accounts; see `scripts/database/roles/01-04` (Render) and
`scripts/database/cpanel/01-03` (cPanel), which also deny the runtime role
CREATE and TEMPORARY on the database. Off-site, write-once backups protect
against the owner role.

## Concurrency and idempotency

* Accounts are locked with `SELECT ... FOR UPDATE` in primary-key order (no
  deadlocks); the balance trigger's UPDATE re-reads the committed balance after
  any wait, so the overdraft check always sees the latest value. Tested with
  concurrent allocations against one campaign: exactly the affordable number
  succeed.
* Each posting needs an idempotency key. A repeat with the same content returns
  the existing entry, without a second audit event; a repeat with different
  content raises `IdempotencyConflictError`. Concurrent duplicates are resolved
  by the unique index (tested: six concurrent identical requests produce one entry).
* External references are unique per source type.

## Audit integration

* `ledger.entry.posted`, `ledger.entry.reversed`, `ledger.account.opened` and
  `ledger.account.status_changed` are business events written in the posting
  transaction: if the audit write fails, the posting rolls back (tested).
* Rejected attempts (`ledger.posting.rejected`, with posting type, reason code
  and source type) are security events recorded outside the rolled-back
  transaction.
* No payment credentials, account numbers or provider secrets are recorded;
  amounts and posting types are.

## Future integration requirements

**Payments (Phase 08):** verify provider webhooks; use the provider transaction
id as the external reference and the idempotency key; post
`advertiser.funding_received` / `membership.payment_received` only after the
provider confirms; settlement and fee postings from provider statements; daily
reconciliation that compares clearing balances with provider reports.

**Advertisers and rewards (Phases 05–07):** campaign records owning their
restricted funding account, linked to one advertiser; verification workflow
with maker-checker; reward rules that call `reward.allocated` with an
idempotency key per verified view or reward decision; campaign closure that
releases unused funds.

**Withdrawals (Phase 10):** KYC and limits before `withdrawal.approved`; payout
via provider with `withdrawal.paid`; failed payouts reversed.

## Assumptions requiring professional review

**Qualified accountant:**
* The chart of accounts, its numbering and classifications.
* Whether advertiser funding is a liability (customer deposit) until campaign
  delivery, and when advertising service revenue is earned.
* Membership revenue recognition (deferred, then recognized per period).
* Whether rewards owed to members are liabilities when allocated or when
  vested, and their tax treatment.
* Whole-shilling amounts (no senti).

**Tanzanian legal and payment specialists:**
* Whether holding advertiser funds and paying member rewards requires a
  licence or approval (Bank of Tanzania, payment system regulations,
  e-money rules).
* Whether advertiser or reward funds must be held in a segregated or trust bank
  account. The ledger's "restricted" classes are internal controls only.
* Consumer protection, gaming or promotion law implications of paid membership
  combined with rewards.
* KYC/AML obligations for withdrawals; tax withholding on rewards.
* Record retention periods for financial records.
