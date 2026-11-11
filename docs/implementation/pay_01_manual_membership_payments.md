# PAY-01 — Production registration and manual Airtel Lipa membership payments

**Status:** implemented and tested locally; **not committed, not deployed, not migrated in production**.
**Baseline:** `main` @ `b3b6d17` (clean working tree).

## 1. What this phase adds

| Area | Behaviour |
| --- | --- |
| Registration | Self-registration (existing email-first flow: register → emailed activation link → member sets password → ACTIVE + email verified) opens only when **all** hold: `DJANGO_RESTRICTED_DEMO=False`, `DJANGO_PUBLIC_REGISTRATION_APPROVED=True`, site configuration "public registration" on. `PUBLIC_REGISTRATION_APPROVED` is now a runtime gate (before: deploy check only) and defaults to `False` outside `DEBUG`. |
| Manual payments | Real membership fees paid by the member to ClipCash's **Airtel Lipa** business number, claimed by the member, **verified by authorized staff in the official Airtel business records**, then posted to the real ledger and activated exactly once. No Airtel API. |
| Configuration | `DJANGO_MANUAL_MEMBERSHIP_PAYMENTS` (default `False`). Refused at runtime and by `check --deploy` (`payments.E003`, `payments.E004`) while restricted demo mode or sandbox payments are on. |
| Recipient details | Business number, recipient name and payment steps are published by a **Super Administrator** in `/manage/membership-payments/instructions/`. Append-only history; nothing hard-coded. |

## 2. Member workflow

1. Plans page / plan page explain that payment is via Airtel Money (Lipa) and verified by staff.
2. **Choose this plan** → existing `choose_plan()` creates a `PENDING` subscription with a snapshot of the administrator-set TZS price → redirect to **Pay** (`/payments/membership/<id>/pay/`).
3. Pay page shows plan, length, **exact amount**, the Lipa business number and recipient name, the administrator's steps, a PIN warning, and the form: amount, paying phone number, Airtel transaction reference, optional payment date.
4. Submit → `submit_claim()` → claim `PENDING_VERIFICATION`. Notification (in-app + email): "Payment submitted for verification". Membership stays `PENDING`; nothing is granted.
5. Status page (`/payments/membership/submissions/<id>/`) shows the claim; member may **withdraw** it while pending.
6. Outcome: **approved** → membership active (notification + email from the existing activation notice; extra in-app "Payment confirmed"); **rejected** → reason shown (notification + email), **Submit corrected details** while the choice is open. Every claim is kept.

Validation (all server-side in `apps.payments.manual`):

* amount must equal the subscription's quoted price exactly (`TZS 5,000`, `5000`, `5,000` accepted);
* phone normalised to E.164 (`0781234567` → `+255781234567`);
* reference: upper-cased, letters/digits plus `. - /` and single spaces, 6–40 characters; the **single-use key** keeps letters and digits only, so `mp 2411.0915-ab12` and `MP24110915AB12` are the same transaction; all-identical-character references refused;
* payment date: not in the future, not before the choice was made, at most 30 days old;
* one open claim per membership choice; an identical repeat returns the same claim (double-click safe); a different second claim is refused;
* a reference already used by an open or approved claim (by anyone) is refused and audited;
* 5 submission attempts per member per hour (PostgreSQL counter);
* verified email and ACTIVE account required (no demo waiver).

## 3. Staff verification workflow

* Queue `/manage/membership-payments/` — oldest pending first; member email, plan, amount, transaction reference, submission time, status; search by claim/transaction reference or email. The overview's attention list shows "Membership payments awaiting verification".
* Detail `/manage/membership-payments/<id>/` — member identity and account state, plan and amount due, reference, phone, stated date, the details the member was shown, other submissions with the same reference, the member's other submissions, audit history, ledger entry once approved.
* **Approve** requires: re-typing the amount and reference **as found in the Airtel records** (not copied from the claim) and ticking the receipt confirmation. Mismatches are refused with instructions to reject instead.
* **Reject** requires a reason (not found, amount mismatch, reference mismatch, different payer phone, already used, other + note).

Permissions:

| Action | Who |
| --- | --- |
| View queue/detail | Super Administrators; staff with `payments.view_manual_payments` or `payments.verify_manual_payments` |
| Approve / reject | Super Administrators; staff with `payments.verify_manual_payments`; never the paying member (service check + DB constraint) |
| Publish / retire Lipa details | Super Administrators only |

### Approval transaction (`approve()`)

One PostgreSQL transaction; locks in the fixed order **member → claim → subscription** (the order every membership change uses):

1. If the claim is already `APPROVED` → return it unchanged (idempotent repeat).
2. Must be `PENDING_VERIFICATION`; reviewer ≠ member; verified amount = claim amount = quoted price; verified reference key = claimed key.
3. Ledger: `membership.manual_payment_received` — **debit CASH, credit MEMBERSHIP_DEFERRED_REVENUE**, external reference = the Airtel reference key (unique per source type), idempotency key `manual-membership:<claim id>`. Posted by the verification process (`actor=None`); the reviewer is recorded on the claim and in the audit event.
4. `memberships.activate_with_manual_payment()` → existing `_activate()`: `activation_source=PAYMENT`, `amount_paid=price`, `payment_reference=AIRTEL-LIPA:<key>` (globally unique), period starts now or after the current period (renewal).
5. Claim → `APPROVED` with reviewer, time, verified amount/reference, receipt confirmation, ledger entry id.
6. Audit `membership.manual_payment.approved`; notifications.

Any failure rolls back everything. Concurrency is proven on multi-connection tests: 4 simultaneous approvals → 1 effect; simultaneous approve/reject → exactly one decision.

A claim submitted while the choice was open is honoured even if verification finishes after `pending_until` or after the plan was withdrawn (quoted terms kept). The expiry job no longer cancels a choice that has a claim under review, and the member/administrator cannot replace or cancel such a choice until the claim is decided.

## 4. Database guarantees (migrations `payments.0004`, `payments.0005`)

* `manual_payment_reference_single_use` — one open/approved claim per reference key, ever.
* One open and at most one approved claim per subscription.
* Approved rows must carry reviewer, time, receipt confirmation, ledger entry, verified amount = amount, verified reference = reference.
* Rejected rows must carry a reason and no ledger entry; reviewer ≠ member.
* Trigger: claims created pending without review data; submitted fields immutable; decided claims final; no deletes.
* Instructions: one active per method; created active; only retirement allowed; no edits in place; no deletes.
* Also unchanged existing guarantees: `sub_payment_reference_unique`, ledger `ledger_entry_external_reference_unique`, balanced entries, funding separation.
* New trigger functions pin `search_path` (RG-03) and are listed in `payments.0005.FUNCTIONS`.

## 5. Separation from sandbox and other financial features

* No `PaymentIntent`, `SandboxPosting`, `MembershipActivationPreview` or sandbox provider is involved; manual payments are refused while `sandbox_active()`.
* `live_payments`, `real_money_rewards`, `withdrawals` remain locked in code (`core.E008` unchanged). Wallets, rewards, withdrawals, advertiser funding and settlement are untouched.
* Complimentary grants remain restricted-demo only, so they cannot coexist with manual payments; complimentary subscriptions keep `activation_source=COMPLIMENTARY`, amount 0.
* The demo email-verification waiver is off whenever manual payments are configured.

## 6. Owner decisions still required (not implemented on purpose)

1. **Refunds / reversals / disputes of approved manual payments.** No automated flow. Today: staff can suspend or cancel the membership with reason `REFUND` / `FRAUD_SUSPECTED`; the ledger entry can be reversed only by a Super Administrator via `reverse_entry` (no UI). Need: refund policy, who approves, how the refund is paid out, ledger treatment (`REFUNDS_PAYABLE`).
2. **Four-eyes approval.** One authorized reviewer approves. Decide whether amounts above a threshold need a second person.
3. **Partial, over- or split payments.** Refused (exact amount only). Decide how to handle money received that does not match (manual refund? credit?).
4. **Revenue recognition.** Fees stay in `MEMBERSHIP_DEFERRED_REVENUE`; recognising revenue over the period (`membership.revenue_recognized`) is still locked under `live_payments`.
5. **Duty roles.** Who holds `verify_manual_payments` (see D2). Recommended: a "Finance Verifiers" group, at least two people.
6. **Verification SLA** and what the member is told; support escalation path.
7. **Forged/squatted references.** A claim using someone else's reference blocks that reference until a reviewer rejects it (then the real payer can resubmit). Staff must compare the paying phone in the Airtel records.
8. **Screenshots/receipts upload.** Not implemented (would never be proof anyway); decide if wanted.
9. Business number, recipient name and plan prices — must come from the owner; nothing is seeded.

## 7. Production configuration checklist (do not apply without authorization)

1. Owner approves public launch; confirm items in §6.
2. Environment: `DJANGO_RESTRICTED_DEMO=False`, `DJANGO_PUBLIC_REGISTRATION_APPROVED=True`, `DJANGO_MANUAL_MEMBERSHIP_PAYMENTS=True`, `DJANGO_PAYMENTS_MODE=disabled`, unset `DJANGO_PAYMENTS_SANDBOX_ALLOWED`, `DJANGO_MEMBERSHIPS_COMPLIMENTARY_GRANTS`, `DJANGO_DEMO_EMAIL_VERIFICATION_OPTIONAL`, `DJANGO_DEMO_ACCOUNTS`.
3. Real SMTP configured (activation and payment emails), `DJANGO_DEFAULT_FROM_EMAIL` on the `.co.tz` domain; cron `run_jobs` running (outbox delivery).
4. `DJANGO_ALLOWED_HOSTS` / `DJANGO_CSRF_TRUSTED_ORIGINS` for the `.co.tz` domain; HTTPS settings per the NetPoa runbook.
5. Back up the database, then `manage.py migrate` (applies `payments.0004`, `payments.0005`; also the pending `core.0004` if the target database lacks it) with the migration (owner) role; re-run the runtime grant script so DELETE stays revoked on the new guarded tables.
6. `manage.py check --deploy --fail-level ERROR` passes (no `payments.E003/E004`, `core.E007`, `core.E008`).
7. In Django admin, Super Administrator: membership plans with owner-approved TZS prices; enable memberships in Site configuration.
8. In `/manage/membership-payments/instructions/`: publish the Airtel Lipa business number and recipient name **exactly** as issued by Airtel; double-check.
9. Grant `payments.verify_manual_payments` to named verifiers (record the assignment).
10. End-to-end rehearsal with a small real payment: register → verify email → choose plan → pay → submit → verify in Airtel portal → approve → membership active → ledger `CASH`/`MEMBERSHIP_DEFERRED_REVENUE` balances match the Airtel statement.
11. Daily reconciliation routine: Airtel statement total = sum of approved claims for the day = CASH postings of type `membership.manual_payment_received`.

---

# PAY-01B — Flexible plans, renewal and final acceptance

## 8. Membership plans (administrator-configured)

* Console: `/manage/plans/` (list with price, length, status, current members, choices awaiting payment and payments awaiting verification), `/manage/plans/new/`, `/manage/plans/<id>/` (edit, activate / deactivate / archive, price-and-length history, audit history). Super Administrators only (existing rule); Django admin keeps working through the same services.
* Fields: name, code (fixed after creation), description, price (whole TZS), length in days, benefits, entitlements, display order, listed / not listed, status.
* Limits: price TZS 1 to `MEMBERSHIPS_MAX_PLAN_PRICE` (default TZS 100,000,000; environment `DJANGO_MEMBERSHIPS_MAX_PLAN_PRICE`); length 1 to 3,660 days (database check). No price or length is assumed anywhere in code; there is no TZS 700,000 or 30-day default.
* Changes take effect for new choices at once (no restart). Each change of price, length, name, benefits or entitlements creates an immutable plan version; audit `membership.plan.created/updated/status_changed`.
* Concurrent edits: the edit form carries the version it showed; if another administrator changed the terms first, the save is refused (`stale_edit`) and the current terms are shown.
* Only `ACTIVE` and listed plans can be chosen (page 404, service refusal). Deactivating stops new purchases only; existing memberships and submitted payments are unaffected.

## 9. Quote protection

A choice snapshots the plan version's name, price, currency, length, benefits and entitlements (database-immutable). The member pays, and the reviewer verifies, the snapshot amount; activation uses the snapshot length. Later plan changes never alter a pending choice, a submitted payment or an active membership. Concurrent choices and plan edits always produce a snapshot equal to a published version (tested).

## 10. Renewal

* A renewal can be chosen within `MEMBERSHIPS_RENEWAL_WINDOW_DAYS` (default 14) of the current expiry; one queued renewal at a time (existing policy, unchanged).
* **Early renewal** approved while the current membership runs: the new period starts at the current expiry and ends at expiry + purchased length (no lost days). The exclusion constraint uses half-open periods, so back-to-back periods are allowed and overlaps are impossible.
* **Renewal verified after the current membership ended**, or a **new purchase after expiry**: the period starts at approval.
* Access checks compare `expires_at` with the current time on every request; they do not depend on the expiry job.

## 11. Problem payments (staff guidance, also on the verification page)

| Situation | Action |
| --- | --- |
| Not yet in Airtel records | Check again later; if it never appears, reject "No matching transaction". The member may resubmit the same reference. |
| Partial or larger amount | Never approve. Reject "amount does not match", note the amount received, then follow the refund procedure. |
| Different paying phone | Confirm with the member through support before approving; reject "different phone number" if it is someone else's payment. |
| Same reference on several submissions | Only one can ever be approved (database rule); reject the others "already used". |
| Member paid twice / paid a closed plan | Approve the matching submission (if any); record the extra Airtel transaction in a support ticket; follow the refund procedure. |

Members are told on the payment page not to submit wrong-amount or duplicate payments and to contact support with the Airtel reference instead.

## 12. Refund and reversal procedure (manual; no automated refunds)

1. **Request:** the member opens a support ticket with the Airtel reference (or staff open one for a mismatched payment).
2. **Check:** a verifier confirms the transaction in the Airtel business records and finds any ClipCash claim for it (queue search by reference).
3. **Decide:** the account owner (Super Administrator) decides; amounts and reasons are written in the ticket. Recommended: a second person confirms before money leaves.
4. **Pay out:** the refund is sent **outside ClipCash** from the Airtel business account by the account owner; the Airtel refund/transfer reference is recorded in the ticket.
5. **Membership:** if the refunded payment had activated a membership, staff cancel it in the console/admin with reason **Refund** (existing `cancel` service; access ends immediately, audited).
6. **Ledger:** only for an **approved** claim, a Super Administrator reverses its `membership.manual_payment_received` entry with `apps.wallets.services.reverse_entry` (exact mirror, once, reason required, audited). There is no console button for this yet. Money that never matched a claim was never posted, so there is nothing to reverse; the ticket is the record.
7. **Reconcile:** the day's Airtel statement must equal approved claims minus reversed entries.

Disputes/chargebacks: handled the same way after the Airtel outcome is known. Owner decisions on refund policy, approval thresholds and ledger treatment of refunds (`REFUNDS_PAYABLE`) remain open; nothing here automates them.
