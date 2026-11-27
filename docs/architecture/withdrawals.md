# Withdrawals and payouts

## FIN-02: real withdrawals paid manually (switched off by default)

Members withdraw their **available** TZS balance (never pending rewards, credits, reserved or restricted
money). Staff send the money outside ClipCash (Airtel Money, M-Pesa, Mixx by Yas, HaloPesa, bank
transfer); ClipCash never initiates an external payment. Code: `apps.withdrawals.real` (workflow),
`services.withdrawal_eligibility` (single eligibility check), `console.py` (staff pages).

### Flow and states

```
REQUESTED ─▶ UNDER_REVIEW ─▶ APPROVED ─claim─▶ PAYOUT_PENDING ─record─▶ VERIFYING ─verify─▶ PAID
   └──── reject / cancel (releases) ────┘          │                      │
                                                    └─ problem ─▶ RECONCILIATION ─verify─▶ PAID
             PAYOUT_PENDING / VERIFYING / RECONCILIATION ─confirm failed (verifier)─▶ FAILED (releases)
```

| Ledger posting | Lines | Feature |
|---|---|---|
| `withdrawal.reserved` | available → reserved (wallet deduction) | `withdrawals` |
| `withdrawal.released` | reserved → available (reject, cancel, verified failure) | `withdrawal_settlement` |
| `withdrawal.paid_manual` | reserved → CASH (net payout) + OTHER_OPERATING_REVENUE (fee) | `withdrawal_settlement` |

Each posting has an idempotency key per request, so it happens once. An uncertain payout is never
released automatically; it stays reserved in RECONCILIATION until a verifier finds the payment (PAID) or
confirms none left ClipCash (FAILED).

### Controls

* **Separation of duties** (also database checks): the member never decides; the approver never claims;
  the operator who recorded the payment never verifies it or confirms its failure.
* **Exclusive claim**: one operator holds a payout (row lock + expected state). A claim never expires into
  a second payment; open claims are reported by the hourly reconciliation job.
* **External references** are unique per channel (`<method>:<key>`, a unique column and the ledger's
  per-source uniqueness); a reference is never proof by itself.
* **Liquidity**: claiming and verifying require recorded CASH ≥ the net payout.
* **Wallet restrictions** block new money movements: a new request, approval and the payout claim. They
  never block recording what already happened: an operator can record a payment already sent, and a
  different verifier can confirm it (PAID, noted as "wallet restricted") or confirm that nothing was sent
  (FAILED, reservation released). A FIN-01 reversal that meets reserved money becomes RECOVERY_REQUIRED
  (nothing negative, no other money taken); if the request is rejected the reward reconciliation job
  completes the reversal, and if the money was already paid out the reward stays RECOVERY_REQUIRED for
  manual recovery with the wallet restricted.
* **Destinations** keep the full number encrypted (Fernet, `DJANGO_PAYOUT_DATA_KEYS`, rotation by listing
  keys newest first); only the operator holding the claim can reveal it, and every reveal is audited.
  Adding one needs the password confirmed again; new destinations wait a cooling-off period
  (`DJANGO_WITHDRAWALS_BENEFICIARY_COOLING_HOURS`, default 24). Each request keeps an immutable snapshot.
* **Identity check** (`WithdrawalIdentity`) is separate from registration and membership: required from a
  configurable amount (0 = always, empty = never), verified by a different staff member; the document
  number is encrypted, staff see its last four digits.
* **Settings** are versioned (`WithdrawalSettings`, append-only): limits, fees (none, fixed, percentage in
  basis points rounded UP to the next shilling; deducted or added), methods, processing time, identity
  threshold, and two pauses (new requests, new payout processing). Requests keep their version and fee.

### Staff roles and permissions

Permissions are granted to staff accounts (or to a group) by a Super Administrator in the console's
"User accounts (admin)" page; no database change is needed. Super Administrators hold every permission,
but the separation rules apply to them too.

| Task | Permission |
|---|---|
| Review, ask for information, approve, reject | `withdrawals.approve_withdrawals` |
| Claim a payout, reveal the destination (claimer only), record the payment, report a problem | `withdrawals.process_payouts` |
| Verify a payment, confirm a failure, resolve reconciliation | `withdrawals.verify_payouts` |
| Change withdrawal settings | `withdrawals.manage_withdrawal_settings` |
| Verify identity checks | `withdrawals.verify_withdrawal_identity` |
| Read-only access | `withdrawals.view_withdrawals` |

**Minimum: two staff identities** besides the member. Person A approves and later verifies; person B
claims, sends and records. The approver can never claim, and the operator can never verify or confirm a
failure of its own payout (database checks). An identity check can be verified by A or by a third person,
never by the member.

### Payout data encryption (operations)

* **Library**: `cryptography==46.0.7` (with `cffi==2.1.1`, `pycparser==3.12`), pinned in
  `requirements/base.txt`. Verified locally on Python 3.13.14 (OpenSSL 3.5.6). Binary wheels exist on PyPI
  for Linux x86_64 with glibc 2.17 or later (`manylinux2014`, abi3 for cryptography, cp313 for cffi), so
  pip needs no compiler or OS packages there. NetPoa's actual architecture, glibc and Python version are
  still to be confirmed during deployment preparation (not checked here).
* **Key generation** (once, on a trusted machine, never in the repository):
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
* **Configuration**: `DJANGO_PAYOUT_DATA_KEYS` in the hosting environment's secret settings (not in
  `.env` files that are copied around, not in logs or tickets). `check --deploy` errors (core.E014) when
  withdrawals are on without a key.
* **Backup and recovery**: store every key that was ever active in the organization's password manager
  or vault, with at least two authorized custodians. Without the key, stored numbers cannot be read and
  payouts to those destinations cannot be sent (members would have to add their destinations again).
* **Rotation**: put the new key first and keep the old keys after it
  (`DJANGO_PAYOUT_DATA_KEYS=new,old`). New values use the new key; existing values keep decrypting with
  the old one. Stored ciphertext is immutable by design (database guards), so old keys must stay listed
  for as long as records encrypted with them must remain readable.
* **Missing or wrong key**: encryption refuses (no destination or identity check can be saved with a
  number), and a reveal shows "the payout data key is not configured" instead of a number; nothing
  falls back to plain text.
* **Leak prevention**: full numbers are never written to logs, audit metadata (a reveal records only the
  withdrawal reference), notifications, emails, ledger descriptions or exports; members and staff see the
  last four digits. The destination and identity forms mark their number fields as sensitive (excluded
  from error reports), and the payout page is not cached.

### Activation and emergencies

| Setting | Effect |
|---|---|
| `DJANGO_WITHDRAWALS` + `DJANGO_WITHDRAWALS_APPROVED` | New requests and payout processing (`withdrawals`). |
| `DJANGO_WITHDRAWALS_APPROVED` alone | Settle existing obligations only: verify recorded payouts, release, reconcile (`withdrawal_settlement`). |
| Settings pauses | Stop new requests and/or new claims without affecting settlement. |

Never active in a restricted demonstration (core.E012). `check --deploy` warns while active (core.W013)
and errors without a payout data key (core.E014). Withdrawals do not need `real_money_rewards`: switching
new rewards off never blocks paying out existing balances.

### Database guards

`wallets.0007` narrowly extends the wallet lock: the reserved account accepts only the three withdrawal
postings, the available account additionally only reserve/release, the pending account only rewards;
wallet transactions stay insert-only mirrors. `withdrawals.0004` replaces the Phase 10 "refuse every
write" lock with: creation as REQUESTED for the member's own wallet and destination, immutable terms,
write-once decisions and references, the transition graph above, ledger links that must be the matching
postings for the wallet deduction, and no deletes; destination secrets immutable; settings and events
append-only; a verified identity is final. PostgreSQL versions of every guard are included but were not
executed locally (MariaDB only).

**Rollback** before any real withdrawal restores the Phase 10 lock. After real withdrawals exist the
reverse migration fails by design: requests, events and ledger entries are financial records.

### Jobs (NetPoa)

`withdrawals.reconcile_real` (hourly, from the cron-driven `run_jobs`; no persistent worker) compares open
reservations with the ledger, checks every paid request has its matching posting, and reports open payout
claims. It never changes a withdrawal or retries a payout. Notifications are written in the request's
transaction; emails go through the existing outbox, also delivered by `run_jobs`.

---

The sections below are the Phase 10 design record (demo workflow and the original contract).

## Phase 10 (demonstration)

This phase adds payout destinations (beneficiaries), real-withdrawal
eligibility (always denied), a separate staff-run **demo** withdrawal workflow,
a provider-neutral payout interface with a sandbox simulator, and failure and
reconciliation handling.

**No real payout exists:**

- no money is sent, and no external request is made;
- no wallet, ledger or sandbox-journal posting is created;
- the Phase 09 wallet posting lock is unchanged;
- real `WithdrawalRequest` rows are refused by the database.

## Beneficiaries (`PayoutBeneficiary`)

There are two destination types:

| Type | Fields | Validation |
| --- | --- | --- |
| Mobile money | Network (Vodacom M-Pesa, Airtel Money, Mixx by Yas, Halotel HaloPesa), holder name | Number normalized with the existing E.164 helper; must be Tanzanian (`+255` plus 9 digits) |
| Bank | Bank code (2–12 letters or digits), holder name | Account number of 6–20 digits |

**Data minimization in place of encryption.** The project has no encryption
library, and Phase 10 never sends real payouts, so the **full number is
validated and then discarded**. Only two things are stored:

- the last four digits, for display (`•••• 5678`);
- a keyed fingerprint, `salted_hmac("clipcash.withdrawals.beneficiary", type:provider:number)` (SHA-256), used to detect duplicates and changes.

There is nothing sensitive to leak; a test checks that the full number
appears in no column.

**Production requirement before real payouts:** store the destination as a
provider token or in encrypted storage (with keys managed outside the
database), restrict who can decrypt it, and record who does so.

**Other rules:**

- **Verification:** UNVERIFIED, or DEMO_VERIFIED from the sandbox provider's demo check, which is labelled "not a real verification". Nothing claims a real account check.
- **Limits:** at most 3 active destinations. At most 5 well-formed add attempts a day, counted by the shared rate-limit table; typing mistakes don't count.
- **Duplicates:** the same destination can't be active twice.
- **Ownership:** members only see and change their own destinations; anything else returns 404.
- **No secrets collected:** there are no PIN, OTP or password fields anywhere.
- **Fixed details:** a trigger keeps a destination's details unchanged once saved. Changing them means adding a new destination, and removing one sets it DEACTIVATED.
- **Snapshots:** requests keep their own copy of the destination, so later changes never alter a request.

## Real withdrawals (always denied)

`services.withdrawal_eligibility(member, amount=None, beneficiary=None)` returns
stable reason codes. It evaluates every rule so members see the full picture:

| Reason | Meaning |
| --- | --- |
| `withdrawals_locked` | Always present: the `withdrawals` financial feature is locked in code. |
| `not_eligible_account` | Only verified, ACTIVE, non-staff members can withdraw. |
| `no_wallet` | The member hasn't opened a wallet. |
| `wallet_restricted` | The wallet is under a restriction. |
| `insufficient_balance` | The ledger-backed available balance is too low; it is TZS 0. |
| `no_verified_beneficiary` | No verified payout destination. |
| `beneficiary_cooling_off` | The destination is less than 24 hours old. |
| `below_minimum`, `above_maximum` | Outside the per-request limits. |
| `daily_limit`, `weekly_limit`, `frequency_limit` | Over the daily or weekly amount, or too many requests. |

The available balance comes **only** from `wallet_services.balances()`, the
ledger projection. Demo rewards and sandbox deposits are never read.

`request_withdrawal()` always raises `WithdrawalsLocked` and audits
`withdrawals.request.denied`. The trigger `withdrawals_request_locked` refuses
every insert, update and delete on `WithdrawalRequest`.

**Limits** (settings, whole TZS) apply to the eligibility rules and to demo scenarios:

| Setting | Default |
| --- | --- |
| Per-request amount | 5,000 – 1,000,000 |
| Daily total | 1,000,000 |
| Weekly total | 3,000,000 |
| Requests per day | 3 |
| Two-approver threshold (high risk) | 500,000 |
| Beneficiary cooling-off | 24 h |

## Demo withdrawals (`DemoWithdrawal`)

Demo withdrawals live in their own table with their own `DEMO_*` states, so a
demo **can never become a real payable request**. Every page says DEMO /
SIMULATED. Only staff with `withdrawals.review_withdrawals` create them, for a
member's existing destination; staff can't create one for themselves.

```
DEMO_REQUESTED → DEMO_UNDER_REVIEW → DEMO_APPROVED → DEMO_PAYOUT_PENDING → DEMO_PAID → DEMO_RETURNED
                                                                     ↘ DEMO_UNCERTAIN → DEMO_PAID | DEMO_FAILED
                                                                     ↘ DEMO_FAILED
REQUESTED/UNDER_REVIEW/APPROVED → DEMO_REJECTED | DEMO_CANCELLED;  REQUESTED/UNDER_REVIEW → DEMO_EXPIRED (job)
```

A database trigger enforces the transitions. It also keeps fixed:

- the member and the destination snapshot;
- the amount, reference, author and idempotency key;
- the risk assessment.

Approvers, the payout initiator and the payout reference are written once.
Decision history (`DemoWithdrawalDecision`) is append-only.

**Separation of duties.** These rules are enforced by both the services and database checks:

- the **approver** must differ from the creator (and from the member);
- **high-risk** requests need two distinct approvers. A request is high risk if it is at or above the threshold or carries any risk flag;
- the **payout initiator** must not be an approver.

So no administrator can create, approve and pay out alone.

**Stale decisions** are refused: every decision includes the status the
reviewer saw. A repeated cancellation does nothing. There's one open demo
request per member (a database rule), and form tokens are idempotency keys.

**Risk flags** never decide anything on their own; they only force two-approver review:

| Flag | Meaning |
| --- | --- |
| `new_beneficiary` | The destination is less than 24 hours old. |
| `unverified_beneficiary` | The destination hasn't passed the demo check. |
| `recent_wallet_restriction` | The wallet was restricted in the last 30 days. |
| `repeated_payout_failures` | 2 or more failed or returned payouts in 30 days. |
| `frequent_beneficiary_changes` | 3 or more destinations added in 7 days. |

## Payout providers

`payouts/base.py` defines the provider-neutral interface:

| Method | Purpose |
| --- | --- |
| `validate_beneficiary` | Check a destination with the provider. |
| `create_payout` | Start a payout. |
| `fetch_status` | The authoritative status of a payout. |
| `validate_callback` | Check that a notice is authentic. |
| `normalize_reference` | Validate and clean a payout reference. |

`payouts/sandbox.py` is the only implementation. Its records live in
`SandboxPayout`, and its callbacks are HMAC-signed with a key derived from
`SECRET_KEY`. It runs behind the same hard gate as sandbox payments
(`sandbox_active()`), so it's off in production.

**Callbacks** (`POST /withdrawals/callback/sandbox/`, signed, CSRF-exempt):

- carry only `event_id` and `reference`;
- are authenticated and then checked against the provider's own record;
- are recorded once per event in `PayoutEvent`, so replays are reported as DUPLICATE;
- are refused if forged (401, audited).

**Failure handling:**

- **Timeouts and outages are not failures.** The status becomes DEMO_UNCERTAIN.
- `withdrawals.reconcile_payouts` (every 5 minutes) asks again with the **same reference**. A payout with an uncertain status is never re-sent with a new one.
- Late or contradictory notices can't reopen a closed request.
- A payout returned after success becomes DEMO_RETURNED.
- **Stuck payouts** (pending or uncertain for more than 60 minutes) are recorded as `WithdrawalException` by the hourly `withdrawals.housekeeping` job, which also expires stale requests after 7 days.

## Interfaces

**Members:**

- `/withdrawals/`: an explanation of why withdrawals are unavailable, the eligibility checklist, the available balance (TZS 0, from the ledger), destinations, a read-only preview (nothing saved), demo scenarios (labelled), a wallet-restriction warning, and security guidance.
- `/withdrawals/destinations/`: add, remove and run the demo check.
- `/withdrawals/demo/<id>/`: a demo scenario's details.

**Console (`/manage/withdrawals/`):**

- **Queue:** search by exact reference or email, status filter, "high risk only", pagination.
- **New demo scenario:** look the member up by exact email, then choose a masked destination.
- **Detail:** masked snapshot, risk, decision history, actions (review, approve, reject, cancel, start payout), simulator (success, failure, timeout, reachable again, returned, replay, forged), payout events and audit history.
- **Exceptions:** list and resolve.
- **Beneficiaries:** masked inspection.

**Permissions:** `view_withdrawals`, `review_withdrawals`,
`operate_payout_sandbox` and `view_beneficiaries`. Super Administrators hold
all four; Content Managers hold none.

## Audit (category `WITHDRAWALS`)

| Area | Events |
| --- | --- |
| Beneficiaries | `withdrawals.beneficiary.created`, `.deactivated`, `.demo_verified` |
| Real requests | `withdrawals.request.denied` |
| Demo requests | `withdrawals.demo.created`, `.duplicate_refused`, `.review_started`, `.approval_recorded`, `.approved`, `.rejected`, `.cancelled`, `.expired` |
| Payouts | `withdrawals.payout.initiated`, `.completed`, `.failed`, `.uncertain`, `.returned` |
| Callbacks | `withdrawals.callback.refused`, `.duplicate` |
| Risk (security holds) | `withdrawals.risk.flagged` |
| Exceptions | `withdrawals.exception.opened`, `.resolved` |
| Simulator | `withdrawals.sandbox.simulated` |
| Security | `withdrawals.action.denied` |

Account numbers, phone numbers, PINs, OTPs and payloads are never recorded (tested).

## Future ledger contract (not implemented)

On a properly funded real system, in a reviewed phase that also lifts the
Phase 09 wallet lock and the `withdrawals_request_locked` trigger:

1. Lock the member's `REWARD_LIABILITY` account (fixed account-lock order) and validate the available balance and every eligibility rule.
2. Create the `WithdrawalRequest`, and post available → `MEMBER_WITHDRAWAL_RESERVED` as one balanced entry, keyed by the request's idempotency key (WITHDRAWAL_RESERVATION).
3. After separated approval, call the real provider once with a fixed payout reference, outside database locks.
4. On authoritative confirmation, post reserved → `WITHDRAWAL_CLEARING` → payout settlement **exactly once**: `withdrawal.paid` is already single-use per related entry.
5. On confirmed failure, release reserved → available once (WITHDRAWAL_RELEASE).
6. If the status is uncertain, reconcile only; never release or re-send.
7. Make corrections and returns through ledger reversal entries, never edits.

## Phase 11 boundary

The member dashboard can show eligibility reasons, destination counts and
demo scenarios. It must never present demo amounts or sandbox figures as
balances or earnings.
