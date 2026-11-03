# 05 — Financial Integrity Audit (Phase 13E)

**Bottom line:** the available evidence supports every one of the 15 financial invariants **for the current
locked phase**. No real-money path exists in code, and the most important locks are enforced by PostgreSQL
triggers and check constraints that the application cannot switch off through settings, feature flags or the
admin. Two qualifications apply throughout:

1. **Database guarantees are only as strong as database role separation.** On the single-owner Render
   configuration, anyone holding the application's database credential can disable triggers (CC-005).
2. **Passing tests are evidence, not proof.** Statements below say which layer enforces each control.

No financial safeguard was changed, and no live or sandbox transaction was executed outside the isolated test
database during this audit.

## 1. Architecture of the financial controls

There are three separate, deliberately disconnected money-like stores:

| Store | Tables | Who writes | Real money? |
| --- | --- | --- | --- |
| **Real ledger** | `wallets_ledgeraccount`, `wallets_journalentry`, `wallets_journalline` | Only `apps.wallets.services.post_entry` / `reverse_entry` | Would be — but every posting type requires a locked financial feature |
| **Sandbox journal** | `payments_sandboxposting` (+ `SandboxAdvertiserFunds`, `SandboxCampaignAllocation`) | Only `apps.payments.services._post` and friends, only when `sandbox_active()` | No; append-only simulation |
| **Demo reward records** | `rewards_demoreward`, `rewards_campaignrewardbudget` | Only `apps.rewards.services` | No; counters, no balances; `apps.rewards` never imports `apps.wallets` |

Member wallets are three real-ledger accounts per member (`REWARD_LIABILITY`, `MEMBER_REWARD_PENDING`,
`MEMBER_WITHDRAWAL_RESERVED`) that a trigger keeps permanently empty.

### Lock layers

| Layer | Mechanism | Evidence | Bypassable by |
| --- | --- | --- | --- |
| L1 Code flags | `real_money_rewards`, `live_payments`, `withdrawals` have **no storage field**; `is_enabled()` returns False for any financial feature | [apps/core/features.py:49-52, 66-69](../../apps/core/features.py#L49-L52) | Code change only |
| L2 Service gates | `post_entry` refuses when the posting type's feature is off; `request_wallet_posting` always refuses; `request_withdrawal` always refuses; `request_funding_status` always refuses; `activate_with_verified_payment` refuses without `live_payments` | [apps/wallets/services.py:317-330](../../apps/wallets/services.py#L317-L330), [wallet_services.py:186-196](../../apps/wallets/wallet_services.py#L186-L196), [advertising/services.py:788-796](../../apps/advertising/services.py#L788-L796), [memberships/services.py:402-405](../../apps/memberships/services.py#L402-L405) | Code change only |
| L3 PostgreSQL | Triggers + check constraints listed in §3 | migrations | DB owner / superuser (CC-005) |
| L4 Deployment checks | `check --deploy --fail-level ERROR` in the build refuses sandbox payments / demo video unless explicitly allowed; `payments.E001` refuses any mode other than `sandbox`/`disabled` | [apps/payments/checks.py](../../apps/payments/checks.py), [scripts/render/build.sh](../../scripts/render/build.sh) | Environment variables (explicit opt-in, sandbox only) |

## 2. Invariant-by-invariant assessment

| # | Invariant | Enforcing layer(s) | Key evidence | Tests | Verdict |
| --- | --- | --- | --- | --- | --- |
| 1 | Membership fees never finance advertiser rewards | **DB (per entry):** `ledger_verify_entry` rejects any entry mixing `MEMBERSHIP` with advertiser/reward classes, and any credit to `REWARD_RESTRICTED` from a non-verified-advertiser source. **App:** posting-type registry has no membership→advertiser path; sandbox `_post` checks posting types; demo rewards use no money | [apps/wallets/migrations/0002_ledger_integrity.py](../../apps/wallets/migrations/0002_ledger_integrity.py) (funding-separation block), [posting_types.py](../../apps/wallets/posting_types.py) | `DirectSqlBypassTests.test_membership_and_reward_funds_never_mix`, `.test_rewards_cannot_be_funded_from_cash` | **Supported.** Note: the DB guarantee is *per journal entry*; multi-entry routing through neutral accounts (e.g. `PAYMENT_CLEARING`) into `ADVERTISER_FUNDING_PENDING` is prevented only by the application posting-type registry (CC-029) |
| 2 | Real rewards require authenticated, reconciled advertiser funding | DB: `ADVERTISER_VERIFIED` credits only from `ADVERTISER_UNVERIFIED`/`REWARD_RESTRICTED`; app: `advertiser.funding_verified` needs a related `funding_received` entry with matching amount, same scope and a **different** actor; L1 lock on `real_money_rewards` | migration 0002; [posting_types.py](../../apps/wallets/posting_types.py) | `DirectSqlBypassTests.test_verified_funds_only_from_pending`; `apps/wallets/tests/test_posting.py` | **Supported** (and currently unreachable: feature locked) |
| 3 | Sandbox payments never become real payments | Sandbox writes only `SandboxPosting`; no call to `post_entry`; `sandbox_active()` requires `PAYMENTS_MODE=sandbox` + (DEBUG or explicit allow); no live mode exists (`payments.E001`) | [apps/payments/services.py:161-176](../../apps/payments/services.py#L161-L176), [providers/__init__.py](../../apps/payments/providers/__init__.py) | `JobAndFirewallTests.test_real_ledger_and_rewards_untouched`, `SandboxGateTests` | **Supported** (application-level separation + absence of any live provider) |
| 4 | Demo rewards never become withdrawable real earnings | `apps.rewards` does not import `apps.wallets`; wallet postings blocked by trigger; withdrawal eligibility never reads demo rewards | [apps/rewards/services.py:1-13](../../apps/rewards/services.py#L1-L13), [withdrawals/services.py:1-15](../../apps/withdrawals/services.py#L1-L15) | `SeparationTests` (wallets), `RealWithdrawalTests.test_real_withdrawals_are_always_denied` | **Supported** |
| 5 | Demo budgets never become real advertiser funds | `Campaign.funding_status` constrained to `UNFUNDED` by a **check constraint**; trigger requires unfunded on insert; `financially_eligible` also needs locked `real_money_rewards` | [apps/advertising/models.py:242, 265-269](../../apps/advertising/models.py#L242), [migrations/0002_integrity.py](../../apps/advertising/migrations/0002_integrity.py) | `test_campaigns.py::test_funding_cannot_be_verified` | **Supported** (DB) |
| 6 | Real wallet balances remain TZS 0 | Trigger `a_wallets_block_member_postings` refuses any journal line on a wallet-linked account; wallet insert requires zero-balance accounts | [apps/wallets/migrations/0005_wallet_integrity.py](../../apps/wallets/migrations/0005_wallet_integrity.py) | `ZeroMoneyTests.test_balances_are_zero_and_projection_uses_ledger_signs`, `.test_database_refuses_postings_to_wallet_accounts_even_with_gates_open` | **Supported** (DB). Scope note: applies to *wallet-linked* accounts only; member-scope ledger accounts not linked to a wallet are not covered (none are created outside tests) |
| 7 | Real wallet posting remains database-blocked | Same trigger + `wallets_transactions_locked` on `wallets_wallettransaction` (any INSERT/UPDATE/DELETE) | migration 0005 | `ZeroMoneyTests.test_posting_entry_point_always_refuses` | **Supported** (DB) |
| 8 | Real withdrawals remain database-blocked | `withdrawals_request_locked` refuses every INSERT/UPDATE/DELETE on `withdrawals_withdrawalrequest` | [apps/withdrawals/migrations/0002_integrity.py:27-32, 128](../../apps/withdrawals/migrations/0002_integrity.py#L27-L32) | `RealWithdrawalTests` (direct `WithdrawalRequest.objects.create` refused) | **Supported** (DB) |
| 9 | Real campaign funding remains disabled | Invariant 5 constraint + `request_funding_status` always refuses + `campaign.funding_committed` requires locked `real_money_rewards` | as above | `test_funding_cannot_be_verified` | **Supported** |
| 10 | No real transfers occur | No real payment/payout provider exists; only `SandboxProvider` and the sandbox payout simulator, both refusing outside `sandbox_active()`; Bunny is the only outbound HTTP integration | [apps/payments/providers](../../apps/payments/providers), [apps/withdrawals/payouts](../../apps/withdrawals/payouts) | `SandboxGateTests` | **Supported** (absence of capability) |
| 11 | Ledger entries remain balanced | Deferred constraint triggers: ≥ 2 lines, debits = credits = total; checked immediately inside `post_entry` via `SET CONSTRAINTS … IMMEDIATE` | migration 0002; [wallets/services.py:269-297](../../apps/wallets/services.py#L269-L297) | `DirectSqlBypassTests.test_unbalanced_entry`, `.test_single_line_and_empty_entries`, `.test_total_mismatch` | **Supported** (DB) |
| 12 | Critical operations remain idempotent | Ledger idempotency key + fingerprint (conflict on reuse); sandbox posting keys; provider event uniqueness; intent/demo-withdrawal form tokens; one `DemoReward` per session; job idempotency keys | services | concurrency tests in 14 `TransactionTestCase` modules | **Supported** |
| 13 | Financial permissions remain enforced | Ledger: only `actor=None` or active superuser; payments/rewards/wallets/withdrawals: `is_staff` + specific `has_perm`; four-eyes rules; services re-check after views | [wallets/services.py:76-84](../../apps/wallets/services.py#L76-L84), [payments/services.py:89-120](../../apps/payments/services.py#L89-L120) | `test_web.py` in each financial app | **Supported** (application level) |
| 14 | Sensitive actions remain auditable | Business events in the same transaction; rejected postings recorded as security events outside the rolled-back transaction; audit table append-only | [wallets/services.py:307-315](../../apps/wallets/services.py#L307-L315), audit migration 0002 | `apps/audit/tests/test_immutability.py` | **Supported**, with CC-005 caveat |
| 15 | No simple feature flag can bypass financial restrictions | Financial features have no `config_field`; `is_enabled` short-circuits; `set_feature`-style service refuses; **and** L3 triggers hold even if L1 were bypassed | [features.py:66-69](../../apps/core/features.py#L66-L69) | `FeatureFlagTests.test_financial_flags_locked_off`, `.test_financial_flags_ignore_any_stored_value`, `.test_financial_flags_cannot_be_set_through_service`; `ZeroMoneyTests…even_with_gates_open` | **Supported** |

## 3. Database-enforced controls (PostgreSQL)

| Migration | Guarantees |
| --- | --- |
| `wallets/0002_ledger_integrity` | Accounts start at 0, identity immutable, not deletable, balance changes only via line insertion (`pg_trigger_depth`); lines must target ACTIVE accounts of the entry currency; posted entries/lines immutable; balanced, ≥ 2 lines, total matches; funding separation; reversal mirrors original, once, never of a reversal |
| `wallets/0005_wallet_integrity` | No posting to wallet-linked accounts; `WalletTransaction` fully locked; wallet identity immutable; restrictions append-style state machine; resolved discrepancies final |
| `withdrawals/0002_integrity` | Real `WithdrawalRequest` fully locked; demo withdrawal terms immutable; approvals written once; state machine; append-only decisions; no deletes |
| `payments/0002_integrity` | Intents created empty; terms/provider evidence immutable; status machine; allocations only to own campaigns; decisions final; postings append-only; no deletes |
| `rewards/0002_integrity` | Policies created as drafts, terms immutable; demo rewards created QUALIFIED/REJECTED, decisions immutable; budget consumption ≤ campaign demo budget; no deletes |
| `advertising/0002_integrity` + model constraint | Funding locked `UNFUNDED`; org/campaign state machines; reviewed fields frozen per revision; no deletes |
| `audit/0002_append_only_trigger` | No UPDATE; DELETE only with `clipcash.audit_purge='on'` |
| `memberships/0003_lifecycle_integrity` | Subscription lifecycle integrity (not money) |

**Known limitations, stated in the migrations themselves:** the table owner or a superuser can disable these
triggers; TRUNCATE is not blocked on the audit table; the purge GUC is not role-restricted. The reviewed template
[scripts/database/production_roles.example.sql](../../scripts/database/production_roles.example.sql) separates an
owner role from a runtime role and revokes UPDATE/DELETE/TRUNCATE on ledger and audit tables — **it has not been
applied anywhere** (CC-005).

## 4. Application-only controls (not guaranteed by PostgreSQL)

* Multi-entry flow-of-funds rules (which posting types exist, required external references, distinct actor for
  verification, same-scope rules) — `apps/wallets/posting_types.py` + `_check_rules` (CC-029).
* Sandbox journal posting-type compatibility and funds-domain tagging — `apps/payments/services.py:_post`.
* Permission checks for every financial staff action.
* Demo reward budget arithmetic is application-locked (`select_for_update`) **and** DB-capped by the rewards
  budget trigger.
* Withdrawal limits, risk flags and beneficiary cooling periods (demo only).

## 5. Concurrency, atomicity and duplicate prevention

* Ledger: accounts locked in primary-key order (deadlock-free); overdraft pre-check plus DB check constraint;
  concurrent duplicate idempotency key resolved via `IntegrityError` → return existing.
* Payments: provider fetch outside transactions, event insert + apply in one transaction, `select_for_update`
  on intents; funds row locked per organisation.
* Rewards: session → budget lock order; one decision per session.
* Viewing: member row lock serialises starts; session row lock per event.
* Withdrawals: `select_for_update` per demo withdrawal; stale-decision `expected_status`. Concurrency coverage is
  thin: `WithdrawalConcurrencyTests` has 2 tests (parallel submissions, parallel payout notices); approval races
  are not covered (CC-021).

## 6. Cross-module financial references

| Reference | Observation |
| --- | --- |
| payments → wallets | Imports only `POSTING_TYPES` for rule checks; never writes ledger tables |
| withdrawals → wallets | Reads wallet balances (always 0) and restrictions for eligibility |
| rewards → wallets | None (by design) |
| payments allocations → rewards | **None.** Demo rewards draw on `Campaign.demo_budget_amount`, an advertiser-entered figure; sandbox deposits/allocations are not consulted (CC-011). Safe, but the demo cannot show "rewards funded by advertiser money" end-to-end |
| memberships ← payments | Preview only; subscription never changed by payments |

## 7. Conclusion

* **Real-money safety in the current phase: strong, multi-layered, and verified by tests that bypass the
  application and hit the database directly** (`DirectSqlBypassTests`, `ZeroMoneyTests`, `RealWithdrawalTests`).
* **Prerequisites before any real-money phase** (not Phase 14 scope): database role separation (CC-005),
  DB-level flow-of-funds rules or reconciliation controls across entries (CC-029), a real provider with its own
  secrets and timestamped/nonced callbacks, stronger anti-fraud (CC-014), monitoring (CC-016), and legal/provider
  approval (Render B7).
