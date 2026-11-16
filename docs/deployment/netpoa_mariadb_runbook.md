# NetPoa production on MariaDB (PRD-00B)

For the owner and whoever deploys ClipCash on NetPoa (cPanel, Passenger, **MariaDB**). Owner decision, 15 Nov 2026:
MariaDB is the production database, because PostgreSQL connections on NetPoa are refused by `pg_hba.conf`.

This runbook **replaces the PostgreSQL steps** of [netpoa_public_runbook.md](netpoa_public_runbook.md) (§0 tools,
§4 driver, §5 provisioning, §6 database values, §7–§9, §15, §18–§19). Every other section there (domain and TLS,
release from Git, Passenger app, static files, cron, SMTP, administrators, the three launch stages, monitoring)
applies unchanged. Database background: [mariadb_port.md](mariadb_port.md).

**Status (15 Nov 2026): prepared and drilled locally only. Nothing on NetPoa has been verified.** The last observed
NetPoa staging build is `fc35cf4` (PostgreSQL); this branch has never run there. §1 lists what must be proven on the
server before any production use. A `STOP` there means do not deploy.

## Ground rules

* Locked in every stage, unchanged: live and sandbox gateway payments, real-money rewards, withdrawals. Nothing here
  unlocks them. Never disable, drop or "temporarily" remove a trigger to make anything pass.
* Two database logins, always: the **migration** login (owns tables and guard triggers; used only for `migrate`,
  `createcachetable`, the login check, backups and restore drills) and the **runtime** login (the application;
  SELECT/INSERT/UPDATE/DELETE only). The runtime login in `.env`; the migration login only in files under
  `~/clipcash/secrets/` (mode 600), loaded for single commands.
* Secrets never on a command line, in a crontab, a log, a chat or a commit. Production is never changed by hand in
  SQL. Every deploy with migrations starts with a verified backup.

## 1. Hosting facts to verify on the server (manual; record each result)

| # | Requirement | How to check | STOP if |
|---|---|---|---|
| M1 | MariaDB 10.6+ (10.11.19 reported) | cPanel → MySQL Databases, or `mariadb --version` / `SELECT VERSION()` | below 10.6 (SKIP LOCKED, CHECK constraints) |
| M2 | Trigger creation allowed | after §3, `check_mariadb_logins --before-migrate`; or create a throwaway trigger in the scratch database | binary logging on, `log_bin_trust_function_creators=0` and NetPoa will not change it: no guards can be created |
| M3 | A runtime user can be limited to SELECT, INSERT, UPDATE, DELETE | cPanel → MySQL Databases → *Add User To Database* → tick only those four | cPanel only offers ALL PRIVILEGES |
| M4 | No hidden grants reach the runtime user (anonymous users, wildcard databases) | `check_mariadb_logins` probes CREATE TEMPORARY TABLE, TRIGGER, DROP, ALTER by behaviour | any FAIL for the runtime login |
| M5 | The driver builds: `mysqlclient` 2.2.7 | `scripts/netpoa/check_host.sh` → "Building the MariaDB driver"; then `pip install -r requirements/mariadb.txt` in the app virtualenv | no `mariadb_config`/`mysql_config`, headers or compiler and NetPoa cannot provide them (alternative drivers need a separate reviewed change) |
| M6 | `mariadb-dump`/`mysqldump` and `mariadb`/`mysql` in the Terminal | `check_host.sh` | missing: no server-side backups (cPanel backups alone are not a tested restore) |
| M7 | Server settings | `SELECT @@lower_case_table_names, @@character_set_server, @@max_allowed_packet, @@wait_timeout, @@max_user_connections` | `lower_case_table_names` ≠ 0 on Linux is fine only if all names are lower case (they are); `max_user_connections` < 10 needs NetPoa |
| M8 | Session settings accepted | `migrate --plan` connects (the backend sets strict `sql_mode`, `time_zone='+00:00'`, READ COMMITTED, utf8mb4 per session) | connection errors about sql_mode or time_zone |
| M9 | cPanel/JetBackup database backups: included? frequency, retention, single-database restore | NetPoa answer + cPanel → Backup | not a STOP (ClipCash keeps its own), but record it |
| M10 | Python 3.13 in Setup Python App; Passenger | `check_host.sh`; Setup Python App page | below 3.10 |
| M11 | Cron every minute; `flock` | cPanel → Cron Jobs; NetPoa answer | longer than 5 minutes (activation/reset emails too slow) |
| M12 | Outbound SMTP 587/465 with TLS; sending limits; SPF/DKIM for the domain | `check_host.sh <smtp host> <port>`; `email_diagnostics --connect` | no TLS route to the provider |
| M13 | HTTPS for the domain; proxy/client-address behaviour | public runbook §2 and §10 | no certificate |
| M14 | Resource limits (memory, entry processes, CPU) with the existing site | cPanel → Resource Usage | faults at idle |

## 2. Files on the server

```
~/clipcash/app/                 the release (application root); .env here, mode 600: RUNTIME login
~/clipcash/secrets/             mode 700
    migrate.env                 MIGRATION login overrides (template: netpoa_migrate.env.example), mode 600
    backup.cnf                  client option file, MIGRATION login (template: netpoa_mariadb_client.cnf.example), mode 600
~/clipcash/backups/             mode 700; dumps, manifests, checksums
~/clipcash/logs/                mode 700
```

`config.settings` reads `.env` without overriding variables already in the environment, so loading
`migrate.env` in a subshell switches only that command to the migration login:

```bash
mig() { ( set -a; . ~/clipcash/secrets/migrate.env; set +a; python manage.py "$@" ); }
```

(The function lives only in the current shell; `migrate.env` itself is never printed.)

## 3. Database and logins (cPanel → MySQL Databases)

1. Create database `<account>_clipcash`. Then in phpMyAdmin (or `mariadb`) **as the migration login**:
   `ALTER DATABASE <account>_clipcash CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;`
   (also for every scratch database you create later).
2. Create user `<account>_ccmigrate` (long random password) → add to the database with: SELECT, INSERT, UPDATE,
   DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES, TRIGGER, CREATE VIEW, SHOW VIEW, LOCK TABLES.
   (cPanel's "ALL PRIVILEGES" also works for this login.)
3. Create user `<account>_ccapp` (different long random password) → add to the database with **only** SELECT,
   INSERT, UPDATE, DELETE. Never CREATE TEMPORARY TABLES, TRIGGER, DROP, ALTER, EVENT, EXECUTE or ALL.
4. Re-check §3.3 whenever someone touches this page: cPanel may re-grant everything when a user is re-added.
5. Write `.env` (from [netpoa_production.env.example](netpoa_production.env.example): `DB_ENGINE=mariadb`, the
   runtime login), `~/clipcash/secrets/migrate.env` and `backup.cnf` (`chmod 600`; `chmod 700 ~/clipcash/secrets`).
6. Virtualenv active: `pip install -r requirements/mariadb.txt` (M5), then
   `python -c "import MySQLdb, django; print(MySQLdb.version_info, django.get_version())"`.
7. `mig check_mariadb_logins --before-migrate` → both logins `OK` (M2–M4). Any FAIL: STOP and fix the grants.

## 4. Staging restore test (before the first production deploy, then monthly)

Proves that a backup made on NetPoa restores on NetPoa with every guard. Uses a scratch database only.

1. cPanel: create `<account>_clipcash_restore` (utf8mb4 as in §3.1) and add `<account>_ccmigrate` to it with the
   §3.2 privileges (and, to test the runtime login, `<account>_ccapp` with the four data privileges).
2. Take a backup of the source (staging or production) database. Once manual payments are live (Stage 3), add
   `CLIPCASH_EVIDENCE_ROOT` so payment receipts (files, not rows) are archived with it (PRD-04):
   ```bash
   cd ~/clipcash/app
   CLIPCASH_DB_OPTION_FILE=~/clipcash/secrets/backup.cnf CLIPCASH_DB_NAME=<account>_clipcash \
   CLIPCASH_EVIDENCE_ROOT=~/clipcash/private/payment_evidence \
       bash scripts/database/mariadb/backup.sh ~/clipcash/backups
   ```
   A failed backup leaves no files behind; a successful one prints `Backup written` and its evidence file count.
3. Restore and verify (refuses unless the target contains `restore`, is empty and utf8mb4, and the checksum matches;
   with an evidence archive it also checks that every receipt the database records is in the archive):
   ```bash
   CLIPCASH_DB_OPTION_FILE=~/clipcash/secrets/backup.cnf CLIPCASH_RESTORE_DB=<account>_clipcash_restore \
   CLIPCASH_PRODUCTION_DB=<account>_clipcash \
   CLIPCASH_RESTORE_EVIDENCE_DIR=~/clipcash/restore/payment_evidence \
       bash scripts/database/mariadb/restore_check.sh ~/clipcash/backups/clipcash-<account>_clipcash-<time>.sql.gz
   ```
   Every line must be `OK` (row-count differences are acceptable only if the site was written to during the backup).
   `CLIPCASH_RESTORE_EVIDENCE_DIR` (empty or new; never the live evidence directory) also extracts the receipts.
4. Application checks against the copy, never production:
   ```bash
   ( set -a; . ~/clipcash/secrets/migrate.env; set +a; export DB_NAME=<account>_clipcash_restore;
     python manage.py migrate --check && python manage.py check_mariadb_logins )
   ```
   Expect `OK migration login`, `OK runtime login`, `OK guards (111 triggers and the lock constraints)` (111 from
   `payments.0008`, PRD-04; 108 before it).
5. Copy the backup files (dump, manifest, checksums and, with Stage 3, the evidence archive) off the server,
   encrypted, then drop the scratch database in cPanel and delete the extracted receipts.
6. Record: date, release, dump size, counts, results.

## 5. First deploy on MariaDB (Stage 1: registration closed, payments off)

Precondition: §1 has no STOP, §3 done, §4 drilled on the scratch database.

1. If the current NetPoa build holds any data that must be kept (it ran PostgreSQL, which NetPoa blocks), STOP:
   transferring it needs the separate, reviewed transfer of [mariadb_port.md §6](mariadb_port.md). A fresh
   database needs no transfer.
2. Release from local Git (public runbook §3): `git archive` of the reviewed commit; record the hash; keep the
   previous release in `~/clipcash/releases/`.
3. Configuration check: `python manage.py check --deploy --fail-level ERROR` (runtime `.env`). Expect only
   `core.W003` (registration closed) and `security.W004` (HSTS 0) as warnings. Email errors
   (`notifications.E004/E005`), `core.E009` or any `payments.E00x` must be fixed first.
4. Migrations as the migration login:
   ```bash
   mig migrate --plan                 # review
   mig migrate --noinput
   mig createcachetable               # the dbcache table (DJANGO_CACHE_URL)
   mig migrate --check                # exit 0
   mig check_mariadb_logins           # OK x3, including "guards"
   ```
5. `python manage.py collectstatic --noinput`, `mkdir -p tmp && touch tmp/restart.txt`; public runbook §10 checks.
6. Cron (cPanel → Cron Jobs; "Cron Email" to someone who reads it):
   ```cron
   * * * * * /bin/bash /home/<account>/clipcash/app/scripts/netpoa/run_jobs_cron.sh /home/<account>/virtualenv/clipcash/app/3.13/bin/python
   */10 * * * * /bin/bash /home/<account>/clipcash/app/scripts/netpoa/jobs_health_cron.sh /home/<account>/virtualenv/clipcash/app/3.13/bin/python
   30 2 * * * CLIPCASH_DB_OPTION_FILE=/home/<account>/clipcash/secrets/backup.cnf CLIPCASH_DB_NAME=<account>_clipcash /bin/bash /home/<account>/clipcash/app/scripts/database/mariadb/backup.sh /home/<account>/clipcash/backups >> /home/<account>/clipcash/logs/backup.log 2>&1
   45 2 * * * find /home/<account>/clipcash/backups -maxdepth 1 -name 'clipcash-*' -mtime +14 -delete
   ```
   No password appears in any line. The first line is mandatory: without it no activation or reset email leaves.
   From Stage 3 (PRD-04), add `CLIPCASH_EVIDENCE_ROOT=/home/<account>/clipcash/private/payment_evidence` to the
   backup line, so receipts are backed up with the database.
7. SMTP acceptance with an owner-controlled mailbox: [prd_01_email_first_authentication.md](../implementation/prd_01_email_first_authentication.md)
   "Production acceptance". Configured SMTP and passing tests are not evidence of delivery.
8. Public runbook §13–§14 (administrators, Stage 1 acceptance); then Stage 2 and Stage 3 exactly as written there.
9. Before Stage 3 (manual payments, PRD-04): create the private receipt directory **outside** `~/clipcash/app`
   (each release replaces that folder) and outside `public_html`:
   `mkdir -p ~/clipcash/private/payment_evidence && chmod 700 ~/clipcash/private ~/clipcash/private/payment_evidence`;
   set `DJANGO_PAYMENTS_EVIDENCE_ROOT` to it in `.env`; `check --deploy` must show no `payments.E005`; allow request
   bodies of at least 6 MB for the payment form in the web server; switch the backup cron line to include receipts
   (§5.6); run §4 once with receipts present. Plans stay unchoosable until manual payments are on and payment details
   are published.

## 6. Later deploys

1. Backup (§4.2) and check the output says `Backup written`. 2. New release into `~/clipcash/releases/<new>`, `rsync`
into `app/` (public runbook §3). 3. `pip install -r requirements/mariadb.txt`. 4. If the release has migrations:
optional maintenance mode (Site configuration), then §5.4. 5. `check --deploy`, `collectstatic`, restart, public
runbook §14 checks. 6. `mig check_mariadb_logins` after every release with migrations.

## 7. Rollback

| Situation | Action |
|---|---|
| Bad code, no migration | `rsync` the previous release back into `app/`, `collectstatic`, `touch tmp/restart.txt` |
| Bad code after migrations | Only if the previous release works with the new schema (additive migrations, as all so far). Otherwise restore (next row) |
| Bad migration or data | **Never reverse or fake migrations; never drop or edit triggers.** Create a new empty utf8mb4 database (`<account>_clipcash_restore_<date>`), run §4.3 into it, check §4.4, point `DB_NAME` in `.env` at it (add both users to it as in §3.2–§3.3), restart. Keep the damaged database untouched for investigation |
| Registration, payment or configuration problem | Public runbook §19 (site configuration switches, `.env` values, maintenance mode, stop the app) |
| Credential exposed | Change it in cPanel, update `.env` / `migrate.env` / `backup.cnf`, restart, `mig check_mariadb_logins`, review the audit log |

## 8. Local drill evidence (15 Nov 2026, disposable databases only)

MariaDB server 10.11.15 (XAMPP, Windows), client tools 10.4.32, release = this branch (PRD-01 + PRD-00B). Database
`test_clipcash` (the disposable test database) created empty, migrated as the migration login, filled through the
real services (10 accounts, 6 payment claims, 4 approved with ledger postings, 1 rejected, PRD-01 email and phone
sign-ups), then:

* `backup.sh`: 65 tables, 108 triggers, manifest and checksums; refusals checked (output inside the repository).
* `restore_check.sh` refusals: a name without `restore`/`test_`, a non-empty target, the production name, a dump
  without checksum.
* Dropped, re-created empty, restored: tables 65 = 65, triggers 108 = 108, every row count equal, guard view
  present, ledger balances = posted lines, entries balanced, locked tables empty; 74 migrations recorded.
* `migrate --check` exit 0; `check_mariadb_logins`: OK migration login, OK runtime login, OK guards (108).
* On the restored copy, as both logins, refused by the guards: changing a posted journal line, deleting an audit
  event, changing an approved payment, deleting a subscription, crediting a ledger balance directly. A member's
  paid membership was active after the restore.
* A dump whose triggers named a login that does not exist here restored with all 108 triggers and the view owned by
  the restoring login.
* The disposable database and the drill's option file were deleted afterwards.

PRD-04 drill (16 Nov 2026, same setup, release with `payments.0008`): 5 accounts, 3 claims (2 with a receipt, 1
approved with its ledger posting).

* `backup.sh` with `CLIPCASH_EVIDENCE_ROOT`: 66 tables, 111 triggers, evidence archive with 2 files, all of them in
  the checksum file. A run made to fail at the archive step left no files at all.
* Restored with extraction: every check `OK`, including "receipts recorded but missing from the archive: 0"; the
  extracted files were byte-identical to the source; the application read both through its storage with matching
  SHA-256; `check_mariadb_logins`: OK x3, guards 111.
* A tampered archive (one receipt removed, checksums regenerated) was refused: "missing from the evidence archive: 1",
  exit 1.
* The disposable database, files and option file were deleted afterwards.

Not proven: anything on NetPoa (§1), restore of a large production-size dump, `mariadb-dump` 10.11 output (the
drill used the 10.4 client).
