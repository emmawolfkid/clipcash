# Configurable manual membership payment methods

Implemented on the accepted MariaDB port, in ClipCash-mariadb on port/mariadb-schema.
The separate main worktree and its CR-01 changes are not edited. No live payment
provider is called, and these tests use fictional records only.

## Administration and member flow

Super Administrators use the existing Payment methods console page to publish, edit,
retire or enable providers. Each provider has a stable code (AIRTEL_LIPA for existing
Airtel records, for example TIGO for another provider), a customer-facing label, a
receiving number, the registered recipient and optional instructions. Keep a provider's
code when changing its number, name or branding: the code identifies its receipts.
Editing an existing row through the console always keeps its code, regardless of
submitted form data. There is one active revision per provider, with multiple providers
active together. Use Edit for an existing provider rather than inventing a new code.

Publishing appends a revision and retires the previous active revision of that provider.
Retirement stops new instruction issuance. Enabling appends an active copy; it does not
rewrite history. Issued versions are immutable and cannot be deleted. Changes, enabling,
retirement and instruction issuance are audited inside their transactions.

A member chooses an active provider for their own pending plan. Issuing instructions
creates an immutable ManualPaymentQuote linking the immutable instruction revision and
the subscription's quoted TZS amount. The form submits this quote ID. A later method edit
or retirement does not change the quote, claim or review screen. Previously issued
quotes can still be submitted until the plan choice expires; pending claims remain
reviewable even when no provider is active. The deployment feature switch still refuses
submission and approval when manual payments are disabled. With multiple active providers,
a submission without a chosen quote is refused.

Reviewers independently enter the receiving number and registered name from the official
provider records, alongside the amount and receipt. These are checked against the exact
issued version, not the current active configuration. Existing view/verify permissions
and the restriction against verifying one's own payment remain in force. Management
remains restricted to Super Administrators, as before.

## Receipt and money invariants

Receipt keys are normalized by the existing parser and single-use within their stable
provider code across every receiving-number revision. Open or approved claims reserve
the receipt. Rejected/cancelled claims may be corrected and resubmitted, as before; an
approved receipt remains permanently consumed.

Existing Airtel claim references, bare ledger receipt keys and AIRTEL-LIPA:<receipt>
activation references are unchanged. Other providers use <code>:<receipt> as the ledger
reference and MANUAL:<code>:<receipt> as the activation reference. The receiving number
and display label never enter these uniqueness keys.

Approval still holds the member, claim, subscription and ledger locks. It posts one
balanced CASH debit / MEMBERSHIP_DEFERRED_REVENUE credit, finalizes the posting on
MariaDB, activates one membership, records the decision and notifies the member in one
transaction. Failed activation or audit recording rolls all those writes back. Repeated
or concurrent approval returns the existing decision without another posting or activation.

The replacement claim/activation guards bind provider receipts to their own claim,
subscription, member and finalized ledger posting. They retain the existing lifecycle,
append-only and overlapping-period rules. The ledger finalization and balance guards
are not weakened.

## Migrations

* payments.0006_configurable_manual_methods adds provider labels (Airtel default for
  existing rows), immutable quote storage and a nullable quote link for legacy claims.
  It replaces the Airtel-only method checks with stable-code validation and changes
  receipt uniqueness to (method, reference_key). It also updates provider-neutral display
  labels. Existing Airtel financial records are not rewritten.
* payments.0007_manual_method_guards replaces the existing affected guards and adds
  quote insertion/immutability guards on MariaDB and PostgreSQL. PostgreSQL functions
  retain pinned search_path; MariaDB retains the migration/runtime login separation.
  There are now 108 MariaDB triggers. Reverse migration is intentionally unavailable,
  since removing these protections while retaining new provider records would be unsafe.
  Apply with application writes stopped: MariaDB trigger replacement uses DDL and is
  not an atomic online operation.

## Validation

The final full MariaDB run applied fresh migrations to the disposable test database
and passed 1,614 tests in 332.213 seconds: zero failures/errors, 69 skips. The database
was destroyed afterward. `makemigrations --check --dry-run` reports no changes;
`git diff --check` passes.

The focused MariaDB run passed 64 tests, no failures/errors, no skips, in 17.185 seconds.
This includes three provider concurrency tests using restricted runtime worker connections:
one approval after a number edit and retirement, blocked reuse after a number edit, and
simultaneous approvals for distinct providers with the same receipt text.

The final restricted-runtime integration run passed 37 tests, no failures/errors or
skips, in 5.872 seconds. It includes all nine configurable-method functional/UI tests,
legacy Airtel guards and raw overlap checks, the phone-first journey, public copy and
console template/accessibility checks. Wrong recipients, foreign quotes and malformed
method IDs are refused; rollback and duplicate approval remain intact.

The in-place upgrade rehearsal passed with real pre-0006 fictional Airtel rows: one
approved claim/posting/activation and one pending claim. Both survived 0006/0007 with
their original IDs, amounts, instruction and receipt references. The restricted runtime
login then reviewed the legacy pending claim against its retired original instruction
details and a retry posted nothing. Its disposable
database was removed. Reproduce using scripts/database/check_manual_methods_upgrade.py;
it refuses an existing test database and never uses the development database for records.

Logs are ignored local artifacts: manual-methods-review.log,
manual-methods-final-runtime.log, manual-methods-final-upgrade.log and
manual-methods-final-full.log.

## Remaining scope and blockers

MariaDB hosting privileges, TLS and backup/restore/cutover verification remain the
accepted port's deployment blockers (docs/deployment/mariadb_port.md). PostgreSQL execution
is blocked: test_clipcash_port is absent and the configured login lacks CREATEDB.
test_clipcash_db remains present and untouched.
Provider verification and refunds remain manual; this feature does not integrate a
payment API or make payments. No commit, push or deployment is performed.

## Feature file inventory

Earlier uncommitted MariaDB-port changes remain in place. This feature changes:

* Payment models, services and forms: apps/payments/{models,manual,forms,views}.py.
* Migrations: apps/payments/migrations/0006_configurable_manual_methods.py and
  0007_manual_method_guards.py.
* Payment tests: apps/payments/tests/{test_manual_methods,test_manual_web,
  test_pay02_acceptance,test_pay03_acceptance}.py.
* Administration: apps/administration/views.py and templates/administration/layout.html;
  templates/administration/membership_payments/{instructions,detail,list}.html.
* Membership activation: apps/memberships/services.py; audit events: apps/audit/events.py;
  provider-neutral labels: apps/core/features.py and apps/wallets/posting_types.py.
* Regression tests: apps/accounts/tests/{test_public_onboarding,test_reg02_phone_first}.py
  and apps/core/tests/test_mariadb_port.py.
* Member/public templates: templates/payments/{manual_pay,manual_claim}.html,
  templates/components/{manual_payment_status,membership_payment_notice}.html,
  templates/members/dashboard.html and templates/public/{home,_footer}.html.
* Upgrade rehearsal: scripts/database/check_manual_methods_upgrade.py.
* Documentation: this file and docs/deployment/mariadb_port.md.
