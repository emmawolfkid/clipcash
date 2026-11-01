# ADR 0001: Financial ledger (Phase 02C) and background processing (Phase 02D)

Status: **Phase 02C accepted and implemented** (see [../ledger.md](../ledger.md)); Phase 02D **accepted and implemented** (see [../background-processing.md](../background-processing.md)).
Real-money features stay locked off in code until legal, payment-provider and
operational sign-off.

## Phase 02C: financial ledger

### Recommendations

1. **Double-entry journal.** `LedgerAccount` (code, type: asset / liability /
   revenue / expense / equity, currency, owner reference), `JournalEntry`
   (UUID, posted_at, description, entry type, idempotency key, reference to the
   business event) and `JournalLine` (entry, account, signed integer amount in
   whole shillings via `apps.core.money`). Every entry has at least two lines.
2. **Balanced postings enforced by the database.** Lines of an entry must sum to
   zero. Enforce with a deferred constraint trigger checked at commit, plus
   service-level validation. Single currency (TZS) per entry.
3. **Separate the money flows into distinct accounts:**
   * membership fees → `membership_revenue` (platform income, never rewards);
   * advertiser deposits → `advertiser_funding:<advertiser>` (liability), moved
     to `campaign_escrow:<campaign>` when a campaign is funded;
   * rewards are paid **only** from `campaign_escrow:<campaign>` into
     `member_rewards_payable:<member>`;
   * withdrawals move `member_rewards_payable` → `payouts_in_transit` → settled.

   A posting-rules table lists the only permitted account-type pairs. No rule
   lets membership revenue fund rewards, so new members' fees can never pay
   earlier members.
4. **Immutable entries.** Journal tables get the same protections as audit
   events (ORM guards, append-only trigger, runtime role without
   UPDATE/DELETE). Corrections are **reversing entries** that reference the
   original; posted entries are never edited.
5. **Idempotency.** Every posting carries a unique idempotency key derived from
   its cause (payment provider transaction id, reward accrual id, withdrawal
   id). A repeat returns the existing entry.
6. **Concurrency control.** Lock the affected account rows (`SELECT ... FOR
   UPDATE`, in a fixed order by id) before checking available balance and
   posting. Balances are computed from lines; a per-account balance snapshot
   (updated in the same transaction) may be added later for speed and must be
   reconcilable to the lines.
7. **Escrow coverage invariant.** Reward accrual must fail if it would make
   `campaign_escrow` negative, checked under lock and backed by a database
   check on the snapshot.
8. **Audit linkage.** Each posting records a business audit event in the same
   transaction (`ledger.entry_posted`, `ledger.entry_reversed`).
9. **Reconciliation.** Daily job comparing provider statements with ledger
   entries; mismatches go to a review queue and alert.

### Open decisions

* Exact chart of accounts and posting rules (needs finance review).
* Whether senti (fractional TZS) will ever be needed; the current convention is
  whole shillings.
* Retention period for ledger and payment records (legal review).

## Phase 02D: background processing

### Constraints

The hosting provider (Netpoa) has **not been verified** to support long-running
worker processes, Redis or a message broker. The design must work on shared
hosting with only cron and PostgreSQL, and scale up later without code changes.

### Recommendations

1. **Database-backed job queue.** A `Job` table (type, payload of identifiers
   only, status, attempts, run_after, locked_until, last_error). Workers claim
   work with `SELECT ... FOR UPDATE SKIP LOCKED`, so several runners never take
   the same job.
2. **Execution model.** A management command `run_jobs --max-seconds 50`
   invoked every minute by cron. If the host later allows a persistent process,
   the same command runs in a loop under a process manager. No Celery or broker
   is required.
3. **Durable email outbox.** Emails are written to an `OutboundEmail` table in
   the same transaction as the business change (so a rolled-back registration
   never sends mail) and delivered by the job runner. This also removes email
   latency from requests (a Phase 01 limitation).
4. **Retries and recovery.** Exponential backoff with jitter, a maximum attempt
   count, then a `FAILED` state visible in the admin with a manual retry.
   Expired `locked_until` returns crashed jobs to the queue. Jobs must be
   idempotent.
5. **Scheduled jobs.** A small schedule table (name, interval, next_run) handled
   by the same runner: reconciliation, token cleanup, audit export/purge,
   expired-session cleanup.
6. **Shared cache.** Use the PostgreSQL database cache (`dbcache://`) unless the
   host provides Redis; rate limiting already requires a shared cache in
   production.
7. **Monitoring.** The runner writes a heartbeat row each run; a health check
   endpoint and admin dashboard show heartbeat age, queue depth, oldest pending
   job and failure counts. Alert when the heartbeat is older than 5 minutes.
8. **Audit.** Job runs use `audit_scope(source="SYSTEM")` so security events
   from jobs are flushed correctly and correlated by run id.

### Open decisions

* Verify Netpoa capabilities: cron frequency, maximum script runtime, persistent
  processes, outbound SMTP limits.
* Email provider (SMTP relay or HTTP API) and its delivery webhooks.
