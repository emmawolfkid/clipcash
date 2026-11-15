# PRD-04: manual membership payment workflow

Local and uncommitted on `port/mariadb-schema` (base `4071d09`). Builds on PAY-01 (manual payments verified by
staff) and PAY-04 (configurable payment methods, immutable quotes). Production payments stay OFF: nothing here
changes `DJANGO_PAYMENTS_MODE=disabled` or turns on `DJANGO_MANUAL_MEMBERSHIP_PAYMENTS`. Test records are fictional.

## Reused (already implemented and tested)

* Plans with administrator-set TZS price, duration, benefits and entitlements; price snapshot on choice.
* Payment methods configured by Super Administrators only, append-only revisions, audited
  (`membership.payment_instructions.published/retired`), several active at once, nothing hard-coded.
* Immutable payment quotes (issued instructions + price), claims with transaction reference, payer phone,
  amount, optional date; single-use references per provider (database unique index); one open and one approved
  claim per membership choice; resubmission after rejection; withdrawal by the member while pending.
* Staff review with independent re-entry of amount, reference, receiving number and recipient; approve/reject
  with reasons; no self-review (database check); audit history on the claim page; queue and search.
* Approval in one transaction: one balanced ledger posting (CASH → MEMBERSHIP_DEFERRED_REVENUE), finalized on
  MariaDB, and one activation with the plan's duration (queued after a current membership); repeated or
  concurrent approval changes nothing (tests in `test_manual_concurrency`).
* In-app notices for submission, approval and rejection; email copies only to a confirmed address, through the
  outbox (no SMS).
* Paying needs a confirmed email address (PRD-01/03A); financial features (live gateways, rewards, withdrawals)
  stay locked in code.

## New in PRD-04

| Area | Change |
|---|---|
| Receipt evidence | Optional upload on the payment form: JPEG, PNG or PDF recognised by **content** (not name or browser type), up to 5 MB (`PAYMENTS_EVIDENCE_MAX_BYTES`); oversized requests refused (413) before they are read. Stored in **private storage** (`DJANGO_PAYMENTS_EVIDENCE_ROOT`) under a random name per claim, owner-only file modes, original file name not kept. SHA-256 recorded. |
| Evidence access | `payments:manual_evidence`: the claim's member and staff with the view/verify payment permission only (others: 404). Always a download with a fixed name, `nosniff`, `no-store`, sandboxed CSP. Staff downloads audited (`membership.manual_payment.evidence_viewed`). |
| Evidence integrity | New table `ManualPaymentEvidence` (migration `payments.0008`): one row per claim; database guards on both engines refuse updates and deletes, and refuse inserts unless the claim is pending and the uploader is its member (3 new MariaDB triggers: 111 in all). |
| Reviewer aid | Staff claim page shows the receipt and warns when the same file was attached to other claims (reused screenshot). Evidence is labelled "not proof"; approval rules are unchanged. |
| Purchase gate | Plans can be browsed by every member (email, phone or username); **choosing a plan is closed** (button hidden, server refuses) until manual payments are enabled AND payment details are published (or sandbox simulation in an isolated test deployment). |
| Channel presets | Console "Payment channel": Vodacom M-Pesa, Airtel Money - Lipa, Mixx by Yas, Bank transfer, or Other. Presets fill only the code and the displayed name; numbers, accounts, names and steps are always typed by a Super Administrator. Bank channels (`BANK...`) show "Account number" and ask for a contact phone. |
| Deploy check | `payments.E005`: with manual payments on, the evidence directory must be absolute, outside `STATIC_ROOT`/`MEDIA_ROOT` and (outside DEBUG) outside the application directory, which each release replaces. |

## Migrations

`payments.0008_manual_payment_evidence`: creates the evidence table and its guards (PostgreSQL function with pinned
`search_path`; 3 MariaDB triggers). No change to existing tables, guards or financial records. Like 0007, the
guards have no reverse migration. Apply as the migration login, with application writes stopped.

## Outstanding decisions

1. Bank transfers: the claim still stores a phone number (used as the member's contact). Recording the payer's bank
   account name/number needs a schema and guard change to claims; decide whether that is needed.
2. Evidence retention: files and rows are kept with the claim indefinitely; set a retention period (privacy) and a
   deletion procedure (would need a reviewed, audited purge like `audit_purge`).
3. Maximum file size (5 MB) and accepted types (JPEG, PNG, PDF).
4. When to approve Stage 3 (owner's written authorization) and which channels to publish first.

## Deployment requirements

* Migration `payments.0008` (migration login), then `check_mariadb_logins` (expects 111 guards).
* Before Stage 3: create the private directory (`mkdir -p ~/clipcash/private/payment_evidence && chmod 700`), set
  `DJANGO_PAYMENTS_EVIDENCE_ROOT`, `check --deploy` without `payments.E005`.
* **Backups:** the database dump does not contain the files. Back up the evidence directory with each database
  backup (e.g. `tar -czf` next to the dump, kept as privately), and restore both together.
* Web server request-size limit (LiteSpeed/Apache) at least 6 MB for the payment form; the application refuses
  anything larger itself.

## Risks and limitations

* Evidence can be forged; it is never accepted as proof. Approval still requires the transaction in the official
  provider records, typed in independently by the reviewer.
* A crash between storing a file and committing the claim can leave an unreferenced file in the private directory
  (never served; harmless, can be cleaned up manually).
* Files are not virus-scanned; they are only ever downloaded (never rendered by the site). Staff should open them
  with up-to-date viewers.
