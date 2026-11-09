# NetPoa compatibility checklist (cPanel)

For the project owner, **before** any ClipCash deployment on NetPoa. Every step reads information only, except
section 3.1, which creates an empty test database and two database users that you delete afterwards.

Nothing here touches the existing NetPoa website, its database or its Python application. Never paste a password
into a chat, report or ticket.

Known so far: PostgreSQL Databases is offered in cPanel; NetPoa support confirmed outbound HTTPS (port 443).
Everything else below is **UNVERIFIED** until you record the evidence.

## 1. Results sheet

Fill in this table as you go. A **STOP** result means do not deploy ClipCash on this hosting as planned (see §5).

| # | Requirement | How (section) | Needed | Result |
|---|---|---|---|---|
| H1 | Python version for a new app | §2.1 | 3.13 (tested). 3.10–3.12 only after the full suite passes locally on that version. Below 3.10: **STOP** | |
| H2 | Passenger Python apps ("Setup Python App") | §2.1 | Available, separate app per domain/subdomain | |
| H3 | `psycopg[binary]` 3.3.6 installs | §2.2 | Installs into the app's virtualenv | |
| H4 | Cron every minute | §2.3 | 1 min. 5 min workable with settings changes (§5). Longer: **STOP** while email is required | |
| H5 | `flock`, `curl`, `psql`, `pg_dump`, `pg_restore` | §2.1 | `psql` and `pg_dump` needed for the database steps and backups | |
| H6 | Outbound SMTP (587 or 465) | §2.4 | Reachable, or the documented no-email waiver | |
| H7 | Resource limits | §2.5 | Room for 2+ Python processes of about 110 MB each next to the existing site | |
| D1 | PostgreSQL server version | §3.2 | **15 or later**. 14 or older: **STOP** | |
| D2 | `btree_gist` | §3.2 | Available and trusted, or installed by NetPoa | |
| D3 | A login that owns the database | §3.2 | Yes, and you can connect as it | |
| D4 | A separate runtime login | §3.2–3.3 | Not the owner, no CREATEROLE/CREATEDB/superuser; separation script passes | |
| D5 | Backups | §4 | `pg_dump` works, or cPanel backups include PostgreSQL; restore drill passes | |
| C1 | Cloudflare in front, direct origin access | §2.6 | Origin can be limited to Cloudflare, or RG-03 hardening is done | |

## 2. Host checks

### 2.1 Python, Passenger and tools

1. cPanel → **Setup Python App** → **Create Application**: note the Python versions in the list, then **cancel**.
   Note whether the page exists at all (it is the Passenger front end).
2. cPanel → **Terminal** (or SSH). Upload `scripts/netpoa/check_host.sh` anywhere outside `public_html`, then:

   ```bash
   bash check_host.sh                      # host only
   bash check_host.sh smtp.example.com 587  # also greets your SMTP server (no login, no mail)
   ```

   It writes nothing and contacts no database. It prints the OS and glibc, the Python interpreters, Passenger,
   `psql`/`pg_dump`/`pg_restore`, `crontab`/`flock`/`curl`, shell limits, disk use and outbound HTTPS to
   pypi.org and video.bunnycdn.com.

### 2.2 Will the database driver install?

`psycopg[binary]` ships compiled wheels that need a reasonably recent glibc (CloudLinux 7 hosts may be too old).
In the Terminal, using the interpreter you would choose:

```bash
/opt/alt/python313/bin/python3.13 -m pip download "psycopg-binary==3.3.6" --only-binary=:all: --no-deps -d /tmp/cc-wheel-check
rm -rf /tmp/cc-wheel-check
```

(Adjust the path to the interpreter `check_host.sh` listed.) A downloaded `.whl` = PASS. "No matching
distribution" = FAIL: ask NetPoa which OS the server runs; the alternative is building `psycopg[c]`, which needs
`libpq` headers and is a separate decision.

### 2.3 Cron

cPanel → **Cron Jobs**. Check that "Once Per Minute (`* * * * *`)" is offered, and **ask NetPoa support** whether a
one-minute schedule is allowed for a script that runs up to 45 seconds. Do not add a cron job yet.

### 2.4 Email

Ask NetPoa: may the account send through an external SMTP provider on 587 (STARTTLS) or 465 (TLS), or must it use
NetPoa's own mail server? Note any sending limit per hour. The optional SMTP argument of `check_host.sh` only
reads the server greeting.

### 2.5 Resources

cPanel → **Resource Usage** (CloudLinux): record CPU ("SPEED"), physical memory ("PMEM"), entry processes ("EP"),
number of processes ("NPROC"), I/O and inodes. The existing Python site uses the **same** limits.

Measured locally (RG-01): one ClipCash process uses about 86–110 MB; one sign-in hashes a password with
1,000,000 PBKDF2 iterations, about 1.2 s of CPU on the reference PC. A one-core limit therefore handles only a
few simultaneous sign-ins.

### 2.6 Cloudflare and the origin

Record: the hostname for ClipCash (use its **own subdomain**, not the existing site's), whether it will be proxied
by Cloudflare (orange cloud), and the server's IP address (cPanel → General Information, "Shared IP Address").

A server reachable directly (bypassing Cloudflare) lets anyone fake their IP address to ClipCash's rate limits
(RG-01 finding S1). Ask NetPoa: can a `.htaccess` file restrict a subdomain to Cloudflare's address ranges, and does
the web server replace the client address with Cloudflare's `CF-Connecting-IP` (mod_remoteip / LiteSpeed "use
client IP in header")? The answer decides which runbook option applies (runbook §9).

## 3. Database checks

### 3.1 Create a throwaway database (the only step that changes anything)

cPanel → **PostgreSQL Databases**:

1. Create database `clipcash_probe` (cPanel will name it `<account>_clipcash_probe`).
2. Create user `ccprobe` (becomes `<account>_ccprobe`) with a generated password. Add it to the database with
   **ALL PRIVILEGES** (this is what cPanel normally does; the checks show what that really grants).

### 3.2 Inspect both logins

Upload `scripts/database/cpanel/00_inspect.sql` to a folder outside `public_html`. It is read-only: it sets the
session read-only and only reads system catalogs.

```bash
# a) the cPanel account's own database login (normally the database owner)
psql "host=localhost dbname=<account>_clipcash_probe user=<account>" -W -f 00_inspect.sql
# b) the added user
psql "host=localhost dbname=<account>_clipcash_probe user=<account>_ccprobe" -W -f 00_inspect.sql
```

`-W` asks for the password, so it never appears in shell history. If login (a) is refused, try the cPanel account
password; if that fails too, ask NetPoa how to connect as the database owner. **Do not** ask NetPoa to make the added
user a superuser.

Read the output:

| Output | Meaning | Needed |
|---|---|---|
| `server_version`, `version_ok` | D1 | `version_ok = t` (15+) |
| `btree_gist` row | D2 | Present with `trusted = t`, or `installed` already set |
| (a) `login_is_or_acts_as_database_owner` | D3 | `t` for login (a) |
| (a) `login_create` on schema public | D3 | `t` |
| (b) superuser / createrole / createdb | D4 | all `f` |
| (b) `member_of` | D4 | must **not** list login (a) |
| (b) `login_create`, `login_temporary` | D4 | Usually `t` on cPanel; step 1 of the cPanel scripts removes them (§3.3) |

### 3.3 Prove the separation (on the throwaway database)

This runs the real ClipCash migrations on the **throwaway** database, so you see the final result before any
deployment. It needs the release, the virtualenv and a `.env` on the server (runbook §3–§6); the exported
`DATABASE_URL` below takes precedence over the one in `.env` for that shell only.

```bash
cd ~/clipcash/app
psql "host=localhost dbname=<account>_clipcash_probe user=<account>" -W \
     -v runtime_role=<account>_ccprobe -f scripts/database/cpanel/01_prepare_database.sql
read -rs -p "Owner URL for the probe database: " DATABASE_URL; export DATABASE_URL; echo
python manage.py migrate --noinput && python manage.py createcachetable
unset DATABASE_URL
psql "host=localhost dbname=<account>_clipcash_probe user=<account>" -W \
     -v runtime_role=<account>_ccprobe -f scripts/database/cpanel/02_grant_runtime.sql
psql "host=localhost dbname=<account>_clipcash_probe user=<account>_ccprobe" -W -f scripts/database/cpanel/03_verify_separation.sql
psql "host=localhost dbname=<account>_clipcash_probe user=<account>_ccprobe" -W -f scripts/database/roles/04_verify_runtime_role.sql
```

The owner URL has the form `postgresql://<account>:<password>@localhost:5432/<account>_clipcash_probe`; a password
with symbols must be percent-encoded. PASS = both verification scripts end with **"passed"**.

Any `STOP:` line from 01, or a failed verification, means genuine separation is not available with these logins.

### 3.4 Clean up

cPanel → PostgreSQL Databases: delete `<account>_clipcash_probe` and the user `<account>_ccprobe`.

## 4. Backups

1. In the Terminal: `pg_dump --version` (from `check_host.sh`). It must be the same major version as the server or
   newer.
2. Ask NetPoa whether cPanel's account backups include PostgreSQL databases, how often, and how long they are kept.
3. A restore drill is done on your own computer (runbook §12): cPanel adds an account prefix to every database name,
   and `scripts/database/restore_check.sh` refuses any database whose name does not start with `clipcash_restore_`.

## 5. Decisions from the results

| Result | Decision |
|---|---|
| D1 fails (PostgreSQL 14 or older) | **STOP.** Django 5.2 refuses PostgreSQL below 14, and on 14 two ClipCash uniqueness rules (`nulls_distinct=False`) are silently not created. Ask NetPoa for PostgreSQL 15+, or use an external database (below) |
| D2 fails | Ask NetPoa to run `CREATE EXTENSION btree_gist;` in the ClipCash database. Otherwise **STOP** (membership overlap protection depends on it) |
| D3 or D4 fails (no owner login you can use, runtime cannot be separated) | **STOP for the cPanel database.** Do not run ClipCash with one all-powerful login: its database protections for audit and ledger history would be bypassable by the application credential. Use an external managed PostgreSQL 15+ where you control roles (then use `scripts/database/roles`), after confirming outbound port 5432 (or the provider's port) from NetPoa |
| H1 is 3.10–3.12 | Run the complete test suite locally on that Python version before deploying |
| H3 fails | Decide on building `psycopg[c]` or another host; do not change the pinned driver ad hoc |
| H4 is 5 minutes | Set `DJANGO_JOBS_HEARTBEAT_STALE_SECONDS=900` and expect email delays up to 5 minutes. Over 5 minutes: **STOP** unless the no-email waiver is used |
| H6 fails | Use NetPoa's relay if permitted, or the no-email waiver (runbook §8) |
| C1: origin cannot be limited to Cloudflare | Restricted deployment waits for RG-03 (proxy allow-list in code) |

Record the completed sheet with the date and the person who ran it. Restricted deployment proceeds only when every
row is PASS or has an owner-approved decision from this table.
