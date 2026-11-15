# DB-01: porting ClipCash to MariaDB 10.11 (NetPoa)

For the owner and the developers doing the port. Branch `port/mariadb-schema`, based on `aed1585`.
Status: **schema, ledger, guard triggers and application SQL ported; operations pending (section 5). Not deployable.**
PostgreSQL stays the default and is unchanged: `DB_ENGINE` defaults to `postgresql`.

## 1. Running on MariaDB

```
pip install -r requirements/mariadb.txt          # base.txt + mysqlclient
# .env: DB_ENGINE=mariadb, DB_NAME, DB_USER, DB_PASSWORD, DB_HOST, DB_PORT=3306, DB_TEST_NAME=test_<name>
# Login separation (§4a): DB_USER = migration login; DB_RUNTIME_USER/DB_RUNTIME_PASSWORD = runtime login
python manage.py migrate                       # as the migration login
python manage.py check_mariadb_logins          # verifies both logins; read-only
python manage.py test apps.core.tests.test_mariadb_port
# The same tests as the RUNTIME login (TestCase classes only; flushing needs TRUNCATE):
CLIPCASH_TEST_AS_RUNTIME=1 python manage.py test apps.audit apps.payments apps.wallets ...
```

In production the application itself runs with `DB_USER` set to the runtime login.

Create both databases as `CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci` (the server default may be
latin1). The test runner accepts PostgreSQL or `config.db_backends.mariadb` only (never Django's stock MySQL
backend, which would silently drop ClipCash's uniqueness rules), and only a distinct `test_*` database.
Only the migration login may create triggers; the runtime login must not (§4a).

## 2. Inventory of PostgreSQL-specific code (at `aed1585`)

| Area | Where | MariaDB status |
|---|---|---|
| Partial unique constraints (`UniqueConstraint(condition=...)`), 29 (32 emulated rules in all, with the next two rows) | models in accounts, advertising, audit, memberships, payments, rewards, videos, viewing, wallets, withdrawals | **Done**: generated columns + UNIQUE index (`config/db_backends/mariadb/schema.py`) |
| Functional unique constraints (`Lower(...)`), 4: user email, advertiser name, invitation email, video category | models | **Done**, same mechanism, exact `lower()` byte comparison |
| `nulls_distinct=False`, 2: wallet discrepancy, withdrawal exception | models | **Done**, same mechanism with NULL sentinel + flag column |
| Exclusion constraint `sub_no_overlapping_periods` (btree_gist, `tstzrange`) | memberships 0001/0002, models | **Done** as a trigger check (weaker under concurrency, see §4) |
| `BtreeGistExtension` | memberships 0001 | No-op on MariaDB (Django's own vendor check) |
| PL/pgSQL guard triggers, 40 functions in 14 migrations | `*/migrations/*integrity*.py`, audit 0002 | **Done**: 102 MariaDB triggers, see §3 |
| Deferred constraint triggers (ledger balance at commit), `SET CONSTRAINTS` | wallets 0002, `apps/wallets/services.py` | **Done**: explicit ledger finalization (§3a) |
| `ALTER FUNCTION ... SET search_path` | core 0004 | Not applicable (MariaDB has no search_path); PostgreSQL only |
| `select_for_update(of=("self",))`, 9 call sites | advertising, payments, rewards, support, videos services | **Done**: `apps.core.locking.lock_own_rows` (identical SQL on PostgreSQL) |
| `select_for_update(skip_locked=True)` | jobs, memberships, payments, viewing, withdrawals | Supported natively (MariaDB 10.6+) |
| Rate-limit upsert (`ON CONFLICT`, `make_interval`, unquoted `key`) | `apps/core/ratelimit.py` | **Done**: `ON DUPLICATE KEY UPDATE` branch |
| `pg_advisory_xact_lock(hashtextextended(...))` | `apps/memberships/services.py` (complimentary grants) | **Done**: InnoDB mutex-row upsert, held until the outer transaction commits or rolls back |
| `SET LOCAL clipcash.audit_purge` | `audit_purge` command, audit trigger | **Done**: authorized purge path (§3b) |
| `LOCK TABLE ... IN SHARE ROW EXCLUSIVE MODE` | `provision_staff_permissions` | **Done**: transaction mutex shared with permission-link write triggers |
| `pg_roles`, `pg_tables`, `pg_proc`, `has_table_privilege`, `pg_trigger`, `pg_constraint` | `administration/summary.py`, `demo_readiness` | **Done**: information_schema, SHOW GRANTS and a definer guard-inventory view |
| `DJANGO_DB_SSLMODE`, `DATABASE_URL` | settings | PostgreSQL only; MariaDB TLS refused for now (**pending**) |
| `scripts/database/*.sql`, `backup.sh`, `restore_check.sh`, `scripts/database/roles`, `cpanel` | scripts | **Pending** (PostgreSQL tools: `psql`, `pg_dump`, roles) |
| Tests using `pg_*` catalogs, `pg_temp`, `SET CONSTRAINTS`, PostgreSQL-gated concurrency suites | `apps/*/tests` | Phase 1 adds `apps/core/tests/test_mariadb_port.py`; the rest are pending |

## 3. Guard triggers

Ported in phase 1 (34 triggers; MariaDB allows one event per trigger):

* memberships: subscription created PENDING only, snapshot immutable, activation data written once
  (PENDING → ACTIVE), state machine, no delete, **no overlapping periods**; plan versions append-only; plan code
  immutable, ARCHIVED terminal, no delete;
* payments (manual Airtel Lipa): claims created pending, submitted details immutable, APPROVED/REJECTED/CANCELLED
  final, no delete; payment instructions created active, append-only, no delete;
* rewards: policies, demo rewards and budgets (consumption never above the campaign's demo budget), no delete;
* advertising: organization and campaign state machines, reviewed details (including the demo budget) frozen
  without a new revision, no delete;
* viewing: watch sessions created empty, identity (member, campaign, revision, video, length, required time)
  immutable, state/outcome/review machines, evidence frozen once closed, revocation, no delete.

Ledger phase (22 more triggers, 56 in all): the ledger (§3a) and wallets 0005 (zero-money lock on member
wallets, wallet identity, restrictions, discrepancies, wallet transactions locked). Approval of an Airtel claim
must point at this claim's finalized posting, and an `AIRTEL-LIPA:` activation needs the finalized posting of
that Airtel transaction (payments 0005, memberships 0003).

Remaining guards (46 more triggers, 102 in all): withdrawals 0002 (real-withdrawal lock on every write,
beneficiaries, demo withdrawals, exceptions, append-only decisions, no deletes), payments 0002 (sandbox payment
intents, allocations to the advertiser's own campaign, refund/dispute/exception decided once, append-only
postings, events and previews, no deletes), support 0002 (tickets, append-only messages and events), videos 0002
(archived is terminal, provider id fixed, no deletes) and audit 0002 (§3b). Every PL/pgSQL guard now has a MariaDB
counterpart; the existing tests of these areas pass on MariaDB both as the migration login and, through the
services and raw SQL, as the runtime login.

### 3b. Audit events: append-only, with an authorized purge path

UPDATE is always refused. DELETE is allowed only when all three hold, which is stricter than PostgreSQL (where any
session may set `clipcash.audit_purge`): the session flag `@clipcash_audit_purge = 'on'` (set and always cleared
by `manage.py audit_purge`); the client login is the schema owner, i.e. the triggers' definer (`USER()` versus
`CURRENT_USER()`), so the runtime login can never purge; and the event is older than the minimum retention (365
days, a frozen copy of `AUDIT_MIN_RETENTION_DAYS`; a test checks they agree). Run `audit_purge` with `DB_USER` set
to the migration login. TRUNCATE needs DROP, which the runtime login does not hold.

### 3a. Ledger finalization (replaces PostgreSQL's commit-time checks)

MariaDB has no deferred triggers. Every entry is therefore written as a `DRAFT` and finalized by one statement,
`UPDATE wallets_journalentry SET status = 'POSTED' WHERE id = ? AND status = 'DRAFT'`
(`apps.wallets.services._finalize`, called by `post_entry` and `reverse_entry`):

* lines can be added only to a draft; a draft and its lines move no balance and back no approval or activation;
* finalization runs the checks PostgreSQL defers to commit (>= 2 lines, debits = credits = total, active accounts
  in the entry's currency, funding separation, exact reversal mirror, referenced entries posted) and applies the
  balances in the same statement, so an overdraft refuses the whole finalization;
* a posted entry and every line are immutable, entries are inserted only as drafts, nothing is deleted;
* an account balance may only ever equal the sum of its lines in posted entries. This is an invariant checked on
  every balance change, not a "finalizing" flag a session could set, so a direct `UPDATE` cannot invent money.

The Airtel approval (`apps.payments.manual.approve`) posts, finalizes, activates and approves in one transaction.
Cost: each finalization re-sums the posted lines of the accounts it touches (indexed); revisit with balance
checkpoints if an account reaches hundreds of thousands of lines.

Conventions (see `apps/core/migration_ops.py`): `check_violation` becomes `SQLSTATE '23000'`, errno 4025
(Django `IntegrityError`); `insufficient_privilege` becomes `SQLSTATE '45000'` (Django `OperationalError`;
PostgreSQL gives `ProgrammingError`, both are `DatabaseError`). Changes are compared as `BINARY` values so a
case-only or trailing-space edit is still a change.

## 4. Known differences and limits

* **Concurrency of trigger checks**: triggers cannot see other transactions' uncommitted rows. Exclusion
  constraints could; the overlap and budget checks rely on the row locks the services take (member lock for
  activations, budget row lock for awards). Unique indexes are fully concurrent-safe.
* **Temporary-table shadowing (RG-03)** is mitigated by login separation (§4a), not by the triggers. PostgreSQL
  pins every guard function's search_path (core.0004); MariaDB has none: a session's
  `CREATE TEMPORARY TABLE advertising_campaign` is what the budget trigger then reads, even when the trigger
  names the table with its schema (verified). Every guard that reads another table (budget, membership overlap,
  ledger finalization and balances, approval and activation links) relies on the runtime login lacking
  `CREATE TEMPORARY TABLES`. `test_temporary_table_cannot_shadow_the_budget_guard` runs as the runtime login.
* **Tests**: PostgreSQL-only hardening tests are skipped on MariaDB; `test_database_hardening`'s
  `DROP TABLE pg_temp...` implicitly committed the test transaction on MariaDB and leaked rows into every later
  test (it was the only leaking class, found by a per-class row-count probe).
* **Reward budget row creation** on MariaDB uses `INSERT ... ON DUPLICATE KEY UPDATE`: `get_or_create`'s
  duplicate-key path deadlocked concurrent awards (3 of 5 failed with error 1213 before the change).
* **Collation**: tables use `utf8mb4_unicode_ci`. Lookups like `email__iexact` rely on it. Plain `unique=True`
  text columns (for example `accounts_user.email`) are therefore case-, accent- and trailing-space-insensitive
  on MariaDB, which is stricter than PostgreSQL. Enum `CHECK (status IN (...))` constraints also accept other
  letter cases on MariaDB (pending: binary collation for status columns). Emulated unique keys use
  `utf8mb4_nopad_bin`, exactly as PostgreSQL compares.
* **Flush** uses `TRUNCATE` (as on PostgreSQL), so tests and `manage.py flush` do not trip the no-delete triggers.
* **Column renames/retypes**: a column used in an emulated unique key cannot be renamed or retyped while the
  constraint exists; such a migration must remove and re-add the constraint.
* **Reverse migrations**: the MariaDB reverse SQL is written but not exercised, because `core.0004` is
  irreversible (as on PostgreSQL).
* **Local server vs NetPoa**: phase 1 was tested on MariaDB 10.11.15 (Windows, XAMPP); NetPoa runs 10.11.19
  (Linux, `lower_case_table_names=0`). ClipCash table names are lower case.

### 4a. Migration and runtime logins

* **Migration login** (owner): creates the schema, guard-inventory view and the guard triggers, which run with its rights (DEFINER).
  Needs SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES, TRIGGER and CREATE VIEW on the database.
* **Runtime login** (what the application runs as): SELECT, INSERT, UPDATE and DELETE only. It must not hold
  CREATE TEMPORARY TABLES, TRIGGER, DROP, ALTER or anything else, from any source.
* `manage.py check_mariadb_logins` (`apps/core/mariadb_logins.py`) reads the runtime login's grants, every role it
  holds (nested, default or not) and PUBLIC, then probes by behaviour: CREATE TEMPORARY TABLE for real with no
  role, the default role and each role; TRIGGER, DROP and ALTER without changing anything.
* **Hidden grants are real.** Classic MariaDB installs (XAMPP included) have anonymous `''@'%'` rows in `mysql.db`
  granting ALL on `test` and `test\_%`. They reached every login on every `test_*` database (the runtime login
  could drop triggers in `test_clipcash`) and appear in neither `SHOW GRANTS` nor `information_schema`; only the
  probes found them. On 14 Nov 2026 the owner approved removing those two rows from the local server (the
  `mysql_secure_installation` step); a dump to restore them was kept outside the repository.
* **NetPoa: deployment blocker until verified.** cPanel usually grants a database user ALL PRIVILEGES (which
  includes CREATE TEMPORARY TABLES, TRIGGER, DROP, ALTER). ClipCash needs two cPanel database users, with
  "Manage User Privileges" set to the lists above, and `check_mariadb_logins` must pass on the server. Also
  unverified there: whether binary logging is on without `log_bin_trust_function_creators` (the migration login
  could then not create triggers; the check reports it), and hidden grants such as anonymous rows.

## 5. Pending phases (proposed order)

1. Application SQL completed: complimentary retry-key mutexes; provisioning write mutex; health/readiness catalogs.
2. Full-suite verification and runtime-login checks are recorded below. PostgreSQL-specific mechanisms stay gated.
3. Operations: backups (`mariadb-dump`), restore drill, the two logins on NetPoa (§4a), TLS.
4. Data transfer (§6) and cutover rehearsal.

## 6. Application data to transfer later

Everything in the authoritative PostgreSQL database at cutover, by table (75 tables):

| Data | Tables | Notes |
|---|---|---|
| Accounts and staff roles | `accounts_user`, `accounts_pendingemailchange`, `accounts_user_groups`, `accounts_user_user_permissions`, `auth_group`, `auth_group_permissions` | Password hashes copy as-is. Permissions and groups must be mapped by natural key (`app_label`, `codename`; group name): the MariaDB ids differ. Check for emails equal under `utf8mb4_unicode_ci` first |
| **Real money (PAY-01)** | `payments_manualpaymentinstruction`, `payments_manualmembershippayment`, `wallets_ledgeraccount`, `wallets_journalentry`, `wallets_journalline` | Must reconcile exactly (counts, sums, balances per account) before and after |
| Memberships | `memberships_membershipplan`, `memberships_membershipplanversion`, `memberships_subscription` | Active periods must not overlap (checked by the new trigger) |
| Advertising, content, viewing, demo rewards | `advertising_*` (6), `videos_video`, `videos_videocategory`, `viewing_watchsession`, `rewards_*` (3) | Bunny video ids copy as-is |
| Wallets and withdrawals (demo, zero money) | `wallets_wallet`, `wallets_walletrestriction`, `wallets_walletdiscrepancy`, `withdrawals_*` (7) | `wallets_wallettransaction` and `withdrawals_withdrawalrequest` must be empty (locked) |
| Sandbox payments | `payments_*` (10 other tables) | Sandbox only; keep or drop by owner decision |
| History | `audit_auditevent`, `notifications_notification`, `notifications_outboundemail`, `support_*` (3), `reports_reportaccess`, `django_admin_log` | Append-only on PostgreSQL; `django_admin_log` needs content type mapping |
| Configuration | `core_siteconfiguration`, `core_scheduledtask` | The singleton row exists after migrate: update, don't insert |
| Do not transfer | `django_session`, `core_ratelimitcounter`, `core_job` (drain first), `core_workerheartbeat`, `django_content_type`, `auth_permission` (re-created by migrate), the cache table | |

Loading rules: the guard triggers refuse rows that were not created through the lifecycle (for example an
APPROVED claim, an ACTIVE subscription or a POSTED journal entry inserted directly), so the transfer must load
into the schema before the triggers are created (or with them dropped), as the migration login, then create the
triggers and verify every constraint. Ledger balances must then equal the sum of posted lines per account
(the invariant the account trigger enforces); check it before the cutover. Timestamps convert from `timestamptz` to UTC `DATETIME(6)`; `jsonb` becomes JSON text.


## 7. Application SQL continuation (14 November 2026)

The health page checks the current login's grants and the guard inventory; demo_readiness
checks all migrated triggers and the financial lock constraints. The inventory is a
SQL SECURITY DEFINER view because the runtime login deliberately cannot inspect triggers
directly. The migration login now also needs CREATE VIEW.

Complimentary grants use a SHA-256 keyed InnoDB row upsert in core_transaction_lock,
before locking the member. The exclusive lock lasts until the outer transaction ends,
including rollback; no session advisory lock is used. Committed keys remain stored.

Staff provisioning takes another mutex row before its locked recheck. Three BEFORE
INSERT/UPDATE/DELETE triggers on auth_group_permissions take that same mutex, so direct
SQL and Django admin writers also wait. This replaces the earlier REPEATABLE READ gap
scan, which did not protect calls nested inside an existing READ COMMITTED transaction.
It works with the normal runtime privileges, adds no implicit commit, and retains the
PostgreSQL table-lock path. There are 105 triggers in total (102 existing guards plus
three permission-link serialization triggers).

These support objects are in the existing, uncommitted MariaDB branch of core.0004.
An already migrated disposable database must be recreated to install them; do not fake
migration history or apply this work to production. Operations and NetPoa verification
remain deployment blockers.


Validation uses only the disposable MariaDB test_clipcash database. The separate main
worktree and PostgreSQL test_clipcash_db are not modified. The main virtualenv lacked
mysqlclient, so the test driver is isolated under the ignored venv/mariadb-deps directory
in this worktree (PYTHONPATH points there).

* Final full suite after fresh migrations: 1,602 tests, 0 failures/errors, 69 skipped,
  399.867 s. The test database was destroyed normally at teardown. Includes permission
  INSERT/UPDATE/DELETE exclusion, concurrent provisioning/retries and runtime workers.
* Focused application/concurrency run: 144 tests, 0 failures/errors, 6 skipped, 103.690 s.
* Restricted runtime-login run: 30 tests, 0 failures/errors, 0 skipped, 4.901 s.
  Covers Airtel approval/retry and rollback after posting or approval, ledger guards,
  health/readiness and provisioning inside an existing transaction.
* Focused concurrency includes owner and runtime worker connections for free grants
  and Airtel approval, concurrent provisioning with exact grants after retries, and
  lock release at commit and rollback while the holding session remains open.
* The migration login purges eligible audit events and records the purge; the runtime
  login is refused by both direct SQL and the real purge command, even with the flag
  set. The flag is cleared on refusal and no success event is recorded.
* PostgreSQL crosscheck is blocked: test_clipcash_port is absent and the configured
  PostgreSQL test login has rolcreatedb=false. test_clipcash_db was only observed in
  pg_database; no query was run against that database.

Files edited in this continuation (earlier uncommitted port changes are retained):

* apps/accounts/management/commands/provision_staff_permissions.py
* apps/accounts/tests/test_provisioning.py
* apps/administration/tests/test_dashboard.py
* apps/administration/tests/test_database_roles.py
* apps/core/migrations/0004_pin_function_search_path.py
* apps/core/tests/test_mariadb_port.py
* apps/memberships/tests/test_complimentary.py
* docs/deployment/mariadb_port.md

No commit, push or deployment was performed.

Logs (ignored local artifacts): mariadb-full.log (initial 1,578-test run: two legacy
catalog assertion errors), mariadb-targeted.log, mariadb-runtime.log and mariadb-final.log.
No MariaDB application/test failure remains in this run. The 69 skipped tests retain
PostgreSQL-specific or other existing environment gates. Deployment still requires the
operations work and host verification in sections 4-6; the documented collation and
direct-SQL concurrency differences also remain.


## Configurable manual membership methods

The subsequent payment-method feature adds payments.0006_configurable_manual_methods
and payments.0007_manual_method_guards. The current inventory has 108 MariaDB triggers
(the accepted port had 105; three immutable-quote guards are added). Legacy Airtel keys
are preserved. See docs/implementation/pay_04_configurable_manual_methods.md for the
provider/quote model, upgrade rehearsal, validation and operational limitations.

Feature validation: fresh MariaDB migrations and the full suite passed 1,614 tests in
332.213 seconds (zero failures/errors, 69 skips). Restricted-runtime integration passed
37 tests in 5.872 seconds; the focused run included three concurrent-provider scenarios.
The pre-0006 upgrade rehearsal preserved approved and pending legacy Airtel rows and
proved runtime approval against retired original details, with no posting on retry.
Disposable databases were removed. PostgreSQL remains blocked by absent
test_clipcash_port and the login's missing CREATEDB privilege; test_clipcash_db is untouched.
