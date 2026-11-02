# Payments and reconciliation (Phase 08), SANDBOX FIRST

This phase covers payment intents, a provider-neutral interface, a local
sandbox provider, authoritative reconciliation, an exception queue, an
isolated sandbox journal, advertiser restricted funds and campaign
allocations, refunds, disputes, and membership activation previews.

**No real money exists in this phase.** Every payment row is `SANDBOX`, which
a database CHECK enforces. There is no live mode (`DJANGO_PAYMENTS_MODE`
accepts only `sandbox` or `disabled`), no real provider, and no real
credentials. Sandbox postings never reach the real ledger. Campaign
`funding_status` stays `UNFUNDED`, memberships aren't activated, and demo
rewards stay non-payable.

## Sandbox gate

`providers.sandbox_active()` is the single hard gate. It's true only when
`PAYMENTS_MODE == "sandbox"` and either `DEBUG` is on or
`DJANGO_PAYMENTS_SANDBOX_ALLOWED=True` (isolated demo or staging only).
When it's false:

- intents, callbacks and the simulator raise `PaymentsDisabled`;
- the callback endpoint and the member payment pages return 404;
- the console payment pages are hidden.

`check --deploy` reports `payments.E002` for sandbox mode in production, and
`payments.E001` for any unknown mode such as "live".

## Funds domains

| Domain | Source | May fund | Ledger accounts used (real chart, sandbox journal) |
| --- | --- | --- | --- |
| `MEMBERSHIP_REVENUE` | Settled membership payments | Membership only; **never rewards** | `PAYMENT_CLEARING`, `MEMBERSHIP_DEFERRED_REVENUE`, `CASH`, `REFUNDS_PAYABLE` |
| `ADVERTISER_RESTRICTED_FUNDS` | That advertiser's settled deposits | That advertiser's own campaigns | `PAYMENT_CLEARING`, `ADVERTISER_FUNDING_PENDING`, `ADVERTISER_FUNDING_VERIFIED`, `CAMPAIGN_RESTRICTED_FUNDING`, `CASH`, `REFUNDS_PAYABLE` |

A payment's purpose fixes its domain and its single payer subject (a
subscription, or an organization), and the database checks this. Advertiser
funds can't come from demo budgets, membership payments or reward records:
the only code path that increases them is the settlement of an
`ADVERTISER_DEPOSIT`. Corrections are new postings, never edits.

## Payment intent lifecycle

```
CREATED → PENDING            provider request created (provider reference written once)
PENDING → PROVIDER_CONFIRMED authenticated notice + authoritative provider status PAID
PROVIDER_CONFIRMED → RECONCILED  every field matches (else: exception queue)
RECONCILED → SETTLED         provider reports settlement
SETTLED → DISPUTED → SETTLED (won) | REFUNDED (lost);  SETTLED → REFUNDED (approved refund)
CREATED/PENDING → CANCELLED (payer) | EXPIRED (job) | FAILED (provider);  PROVIDER_CONFIRMED → FAILED (rejected after review)
```

A database trigger enforces the transitions. It also keeps every term of the
payment fixed: purpose, domain, payer, subject, amount, currency, reference,
environment, provider and idempotency key. Provider evidence is written once.

**Validation:**

- **Amounts:** whole TZS only (no zero, negative, fractional or float amounts). Deposits must be between TZS 1,000 and TZS 100,000,000.
- **Membership payments** always use the subscription's quoted price; the browser sends no amount. A subscription can have one open payment at a time.
- **Deposits** can only be made by owners of approved advertisers, and staff accounts can't make them.
- **Form tokens** are idempotency keys, so a double submit returns the same payment.

## Provider interface and sandbox

`providers/base.py` defines the provider-neutral interface:

| Method | Purpose |
| --- | --- |
| `create_payment` | Ask the provider to start a payment. |
| `fetch_status` | The authoritative status of a transaction. |
| `validate_callback` | Check that a notice is authentic. |
| `normalize_reference` | Validate and clean a provider reference. |
| `refund` | Ask the provider for a refund. |

A real provider is added later as another implementation, without changing
`services.py`.

`providers/sandbox.py` simulates an external provider:

- its "external" records live in `SandboxProviderTransaction`;
- callbacks are HMAC-SHA256 signed with a key derived from `SECRET_KEY`;
- **callbacks carry only `event_id` and `provider_reference`.** Bodies with any other field are rejected, so amounts and statuses can't be injected even with a valid signature.

The staff simulator (`simulator.py`, requires `payments.operate_sandbox`) acts
as the provider. It offers: pay, pay the wrong amount, fail, settle,
chargeback, replay, unknown reference, and forged signature. Each action
changes the simulated provider record and then delivers a signed callback
through the **same** `receive_callback` path. It never sets ClipCash statuses
itself.

## Reconciliation (`services.process_provider_update`)

1. **Authenticate the notice:**
   - a bad or missing signature returns 401 and is audited;
   - malformed notices are refused;
   - more than 120 callbacks a minute are rate-limited;
   - bodies over 4 KB are refused.
2. **Fetch the authoritative provider status** with no database lock held.
3. **Record the event exactly once:** a unique `(provider, event_id)` means a replay is recorded as DUPLICATE and changes nothing.
4. **Compare** the environment, the intent reference, the currency and the amount. For memberships, the subscription must still be awaiting payment and must not have been paid already.
5. **On any mismatch,** open a `ReconciliationException`: the payment stays PROVIDER_CONFIRMED and nothing is posted. The exception reasons are:
   - `unknown_reference`;
   - `amount_mismatch`, `currency_mismatch`, `reference_mismatch`, `environment_mismatch`;
   - `intent_closed`;
   - `duplicate_payment`, `membership_not_pending`;
   - `funds_shortfall`, `funding_inconsistent`.
6. **Review actions never create money:**
   - **retry** re-checks the provider's own record;
   - **reject** closes the payment as FAILED, and any money is returned outside ClipCash;
   - **dismiss** records a note.

## Sandbox journal (isolated)

The Phase 02C ledger has no sandbox/live dimension, so it can't safely hold
test money. `SandboxPosting` is a separate, append-only journal:

- **Balanced by construction:** each row is one debit/credit pair of a single amount, so the sum of all balances is always 0. The console shows this.
- **Same rules as the real ledger:** each posting uses a real-chart posting type and is validated against that type's allowed debit and credit accounts, so the real funding-separation rules apply.
- **Idempotent:** each posting has a unique key.
- **Never imported** into `apps.wallets`.

| Event | Posting(s) |
| --- | --- |
| Membership reconciled | `membership.payment_received`: PAYMENT_CLEARING → MEMBERSHIP_DEFERRED_REVENUE |
| Deposit reconciled | `advertiser.funding_received`: PAYMENT_CLEARING → ADVERTISER_FUNDING_PENDING (advertiser) |
| Settled | `payment.settled`: CASH ← PAYMENT_CLEARING; deposits also `advertiser.funding_verified`: PENDING → VERIFIED |
| Allocation / release | `campaign.funding_committed` / `campaign.funding_released`: VERIFIED ⇄ CAMPAIGN_RESTRICTED_FUNDING (campaign) |
| Refund / chargeback lost | `*.refund_approved`: (deferred revenue or VERIFIED) → REFUNDS_PAYABLE, then `refund.paid`: REFUNDS_PAYABLE → PAYMENT_CLEARING |

**Revenue recognition** (deferred → earned) is out of scope, because no
membership is activated by sandbox money.

**Deviation from the real ledger:** in the real ledger,
`advertiser.funding_verified` needs a second person. In the sandbox, the
provider's settlement report serves as the independent verification. Live
funding must restore two-person verification.

## Advertiser restricted funds

- **`SandboxAdvertiserFunds`** holds received, allocated, frozen and refunded amounts per advertiser. Unallocated funds are received − allocated − frozen − refunded, and a database check keeps that from going negative.
- **Allocations** (`SandboxCampaignAllocation`):
  - only an owner can allocate, and only to their **own** approved campaign; a trigger also refuses other advertisers' campaigns;
  - one active allocation per campaign;
  - the amount can't exceed unallocated funds;
  - concurrent allocations are serialized by a row lock.
- **Release** returns the amount to unallocated funds.
- **The campaign's real `funding_status` is never touched.**

## Refunds and disputes

- **Refunds:** full amount only.
  - A staff member requests it; for deposits, the amount is frozen immediately so it can't be reused.
  - A **different** staff member approves or rejects it. This is enforced by the service and by a database check.
  - Approval calls the provider's refund (local in the sandbox), posts the reversal and marks the payment REFUNDED.
  - Funds already allocated must be released first (`funds_committed`).
- **Disputes / chargebacks:** a provider chargeback notice opens a dispute.
  - It freezes whatever is still unallocated.
  - Any remainder that's already committed becomes a `funds_shortfall` exception, and the affected allocations are flagged. Funds never go negative.
  - If the dispute is won, the funds are unfrozen. If it's lost, the frozen amount is refunded and the shortfall stays open for manual action.
  - The person who resolves a dispute must be different from the person who opened it.
- **Memberships:** access is **not** clawed back on a refund or chargeback, because no business policy exists for that yet.

## Membership payments (preview only)

Phase 03 subscriptions can't distinguish sandbox entitlements from paid ones,
so a settled sandbox membership payment creates a
`MembershipActivationPreview` instead of changing the subscription:

- **WOULD_ACTIVATE**, with the start and end dates it would have;
- **WOULD_RENEW**, starting when the current period ends;
- **DUPLICATE_PAYMENT** or **NOT_PENDING**.

At most one "would activate/renew" preview can exist per subscription (a
database rule), so a second payment can never extend a membership twice.
Real activation stays behind `activate_with_verified_payment` and the locked
`live_payments` flag.

## Jobs

| Job | Interval | What it does |
| --- | --- | --- |
| `payments.expire_intents` | 60 s | Expires unpaid intents (SKIP LOCKED). |
| `payments.poll_pending` | 300 s | Asks the provider about pending, reconciled and recent settled payments, to catch lost callbacks and chargebacks. Non-transactional, with backoff on retries. |
| `payments.check_funding` | 1 h | Recomputes advertiser funds from their source records; mismatches go to the exception queue. |

## Interfaces

**Members:**

- `/payments/`: memberships awaiting payment, a "Start sandbox payment" button, and payment history.
- `/payments/intent/<id>/`: the payment's status, provider updates, the membership preview, and a cancel button.

**Advertisers:** `/payments/advertisers/<org>/funding/`, shown as the "Funding (sandbox)" tab. It has the fund totals, deposits, allocations, and allocate/release actions.

**Callback:** `POST /payments/callback/sandbox/`. It's CSRF-exempt because a provider calls it rather than a browser, and is authenticated by the `X-Sandbox-Signature` HMAC instead.

**Console (`/manage/payments/`):**

- payment list and detail: simulator, refunds, disputes, provider events, sandbox postings, exceptions, audit history;
- the exception queue;
- funding, allocations, and sandbox journal balances.

Every page is labelled **SANDBOX**.

**Permissions:** `payments.view_payments`, `operate_sandbox`,
`review_reconciliation` and `approve_refunds`. Super Administrators hold all
four; two-person rules need two different administrators. The Django admin is
read-only.

## Audit (category `PAYMENTS`)

| Area | Events |
| --- | --- |
| Intents | `payments.intent.created`, `.cancelled`, `.expired`, `.failed` |
| Callbacks | `payments.callback.accepted`, `.refused` (security), `.duplicate` (security) |
| Reconciliation and settlement | `payments.reconciliation.completed`, `.refused`; `payments.settlement.completed`; `payments.sandbox.posted` |
| Exceptions | `payments.exception.opened`, `.resolved` |
| Allocations | `payments.allocation.created`, `.released`, `.refused` (security) |
| Refunds | `payments.refund.requested`, `.completed`, `.rejected` |
| Disputes | `payments.dispute.opened`, `.resolved` |
| Memberships | `payments.membership.preview` |
| Simulator | `payments.sandbox.simulated` |
| Security | `payments.action.denied` |

Only references, reason codes and whole-TZS amounts are recorded: never
payloads, signatures, keys or personal data.

## Phase 09 boundary

Wallets must not read sandbox records as balances. Before any real money
exists, all of these are needed:

- a real provider implementation;
- live mode with its own review;
- two-person funding verification;
- relaxing `adv_campaign_funding_locked` together with evidence of reconciled funding;
- posting through the real `apps.wallets` journal.
