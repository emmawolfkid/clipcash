# Phase 15-A — Notifications

**Status:** implemented, uncommitted, awaiting owner review · **Baseline:** `main` @ `5852b4d`
**Audit finding addressed:** CC-009 (notifications limited to four account emails). Original roadmap item
ORQ-13.1 (Notifications) and ORQ-13.2 (membership expiry reminders). No financial code, table or lock changed.

## 1. Architecture

```
business service (inside its transaction)
   └── apps.notifications.inbox.notify(recipients, kind, category, title, message, link, dedupe_key, email=?)
         ├── Notification row per recipient  (savepoint insert; UNIQUE(recipient, dedupe_key))
         └── email=True → existing outbox: enqueue_email("notifications.notice", {notification: id})
                              └── existing job notifications.deliver_email (retries, failure hook, audit)
```

* **Reused, not duplicated:** the email outbox, delivery job, retry/backoff, failure audit, `email_diagnostics`,
  the job registry and schedules. There is no second email queue.
* **Transactions:** notices are written in the producer's transaction, so they exist only if the business change
  commits and vanish with it on rollback. Email delivery happens later in the worker; a delivery failure can never
  undo a business change.
* **Duplicates:** `dedupe_key` identifies the event (e.g. `campaign.approved:<id>:<revision>:<from>:`) and is
  unique per recipient in PostgreSQL. Each insert runs in its own savepoint, so a duplicate (retry, repeated job,
  replayed request, concurrent producer) neither breaks the caller nor queues a second email — only the writer
  that created the notice queues its email (tested with four parallel PostgreSQL connections).
* **Dependency direction:** business apps import `apps.notifications.inbox`; notifications never imports them.
  Job failures reach notifications through a new `job_failed` signal sent by `apps.core.jobs.services`.

### Data model (one additive migration)

`notifications/0002_in_app_notifications.py` — **CreateModel `Notification` only**; no existing table altered.
Fields: recipient (FK, CASCADE), category, kind, title (120), message (400), link (200), dedupe_key (160),
read_at, created/updated. Constraints: `notification_once_per_event` (UNIQUE recipient + dedupe_key),
`notification_category_valid`, `notification_link_internal` (link is "" or "/…", never "//…" or containing "\\"),
`notification_title_not_blank`. Indexes: recipient + created_at; partial index on unread. The migration was applied
only to the isolated test database by the test runner; **it has not been applied to the development database**
(run `python manage.py migrate` when you accept the phase; Render's `predeploy.sh` applies it on deploy).

## 2. Implemented events

Only events that exist in the current business services. Texts state no amounts, reasons, reviewer notes, fraud
signals, tokens or other people's data; reward and withdrawal texts always say "simulated"/"demo".

| Area | Event (kind) | Recipient | Email | Producer |
| --- | --- | --- | --- | --- |
| Account | `account.email_confirmed` | the account | — | `accounts.services.confirm_email` |
| Account | `account.password_changed`, `account.password_reset` | the account | — (existing auth emails unchanged) | password change / reset views |
| Account | `account.reactivated` | the account | — | `log_access_change` (suspended/disabled → active) |
| Membership | `membership.complimentary`, `membership.activated` | member | yes | `_transition` |
| Membership | `membership.reinstated`, `membership.choice_expired` | member | — | `_transition` |
| Membership | `membership.suspended`, `membership.cancelled` (by staff), `membership.expired` | member | yes | `_transition`, `process_expiry` |
| Membership | `membership.expiring` (reminder) | member | yes | job `memberships.expiry_reminders` (hourly) |
| Advertising | `advertiser.review_needed` | staff with `advertising.review_advertisers` | — | `create_organization`, `resubmit_organization` |
| Advertising | `advertiser.approved / rejected / suspended / reinstated` | organization team | yes (not reinstated) | `review_organization` |
| Advertising | `advertiser.invitation` | invitee, if an active account uses the address | — | `invite_member` |
| Advertising | `campaign.submitted`, `campaign.activated`, `campaign.completed` | team | — | `_event` |
| Advertising | `campaign.review_needed` | staff with `advertising.review_campaigns` | — | `_event("submitted")` |
| Advertising | `campaign.approved`, `campaign.rejected` | team | yes | `_event` |
| Advertising | `campaign.paused / resumed / cancelled` — **only when done by staff** | team | yes (not resumed) | `_event` |
| Videos | `video.ready`, `video.failed` | the video's creator (or video managers) | — | `sync_status`, `report_upload_failed` |
| Viewing | `viewing.review_confirmed / review_rejected / revoked` | member | — | `review_session`, `revoke_verified_session` |
| Viewing | `viewing.review_needed` (**at most one per reviewer per day**) | staff with `viewing.review_viewing` | — | `_close` → suspicious |
| Rewards | `rewards.recorded`, `rewards.reversed` (simulated) | member | — | `evaluate_session`, `_reverse_locked` |
| Rewards | `rewards.policy_review_needed` | approvers **other than the author** | — | `create_policy` |
| Demo withdrawals | `withdrawals.demo_requested / approved / rejected / cancelled / paid / failed / expired / returned` | member | — | `_decide` |
| Demo withdrawals | `withdrawals.review_needed` | reviewers other than creator and member | — | `_decide("created")` |
| Operations | `operations.job.failed` (job type only, never payload or error text) | staff with `core.view_operations` (Super Administrators) | — | `job_failed` signal |

Not notified on purpose: members' and advertisers' own actions (cancelling a choice, own pause/withdraw/archive),
rejected reward decisions, every viewing heartbeat or session (spam), sandbox payments (not in scope).

## 3. Recipient authorization and safety

* Inbox and actions are limited to `recipient = request.user`; another account's notice is 404 and unchanged.
* Mark-read / mark-all / open are POST with CSRF; GET returns 405.
* Links: internal paths only (service check + database constraint), re-checked before `open` redirects; the
  inbox `next` parameter accepts only `/notifications/...` same-site paths.
* Team notices go to active members of that organization only; staff notices to staff holding the specific
  permission. Inactive accounts receive nothing.
* Read-only Django admin for notices, Super Administrators only (they can mention members' activity).
* No audit event for reading notices: reading or marking one's own notices is not a sensitive action; every event
  that produces a notice is already audited by its producer.
* Rate limiting was not added: the actions only change the caller's own rows and are idempotent.

## 4. Delivery, retries and diagnostics

* Notice email: subject "ClipCash: <title>", body = title, message and "sign in to see details" — **no link, token
  or account data**. Skipped (`SKIPPED`, reason recorded) if the notice was removed or the account is inactive.
* Retries/backoff/failure: the existing `notifications.deliver_email` job (6 attempts); a terminal failure marks
  the email FAILED, records `notifications.email.failed`, and — new — alerts operators in-app via `job_failed`.
* SMTP not configured: emails stay in the outbox (console backend locally); `check --deploy` warns
  (`notifications.W001`); `email_diagnostics` reports the configuration.
* Retention: `notifications.prune_notifications` (daily) deletes **read** notices older than
  `NOTIFICATIONS_RETENTION_DAYS` (365); unread notices are kept.

## 5. Membership expiry reminders

`memberships.send_expiry_reminders()` via job `memberships.expiry_reminders` (hourly, retryable): ACTIVE
memberships ending within `DJANGO_MEMBERSHIPS_EXPIRY_REMINDER_DAYS` (default 7; 0 disables) get one reminder
(event key per membership) unless a renewal is already scheduled. It never renews, charges or changes anything.

## 6. Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| `DJANGO_MEMBERSHIPS_EXPIRY_REMINDER_DAYS` | 7 | Reminder window; 0 disables |
| `NOTIFICATIONS_RETENTION_DAYS` (code) | 365 | Read notices kept this long |
| Existing `DJANGO_EMAIL_*`, worker | — | Email copies need SMTP and the worker; in-app notices do not |

## 7. User-facing pages

| URL | Purpose |
| --- | --- |
| `/notifications/` | Inbox for every role: category filter, unread-only filter, 20 per page, empty states |
| `POST /notifications/<id>/open/` | Mark read and go to the related page |
| `POST /notifications/<id>/read/`, `POST /notifications/read-all/` | Mark read |
| Header bell (all pages, signed in) | Unread count badge; accessible label "Notifications, N unread" |
| Member menu "Notifications", console menu "My notifications" | Navigation with unread count |

Responsive layout and styles in `static/css/clipcash.css` (no inline CSS or JavaScript).

## 8. Tests

`apps/notifications/tests/test_inbox.py` (19) and `test_events.py` (17): recipient, inactive accounts, duplicate
prevention (sequential and 4-way PostgreSQL race), rollback, duplicate inside a caller transaction, link safety
(service and database), notice email content and skip, inbox filters/empty states/pagination/bell, mark one/all,
open redirect, unsafe `next`, cross-account 404, POST/CSRF, sign-in, staff inbox; account notices with unchanged
auth emails; complimentary grant notice and email; expiry reminders idempotent, renewal skip, window, disabled,
job retried twice; expiry/suspension without reasons; members' own actions silent; SMTP failure leaves the grant
active and the email pending; campaign lifecycle to team and reviewers with organization isolation; staff vs own
pause; organization decisions without reasons; reward/revocation notices simulated without amounts; four-eyes
policy notice; demo withdrawal notices; job failure alert once to operators; video failure to the creator.

Two existing query-count tests were adjusted: the test client delivers queued email after each request, so notice
emails queued during setup were being delivered inside the measured request. The tests now deliver them first and
still assert that the page's own query count does not grow.

## 9. Verification

Executed on the isolated `test_clipcash_db` (development database only read; the new migration is **not**
applied to it).

| Command | Result |
| --- | --- |
| `python -W default manage.py test apps --keepdb -v 2` — first run | 1003 run: 1002 passed, **1 failed** (`viewing…ConsoleTests.test_list_has_no_n_plus_one`: test-client email delivery inside the measured request, same cause as the two query-count tests in §8); fixed the same way |
| `manage.py test apps.viewing --keepdb` after the fix | 39 run, OK |
| `python -W default manage.py test apps --keepdb -v 2` — **final run** | **1003 run, 1003 passed, 0 failures, 0 errors, 0 skipped.** Reported time 27 152.8 s: the machine was apparently asleep or heavily loaded during the run — the previous run of the same code took 897.5 s |
| Financial-integrity classes in the final run | `DirectSqlBypassTests` 14, `ZeroMoneyTests` 3, `RealWithdrawalTests` 2, `FeatureFlagTests` 9, `FinancialInvariantTests` 2 — all passed |
| Focused `apps.notifications` | 55 run, OK (incl. the 4-connection duplicate race) |
| `manage.py check` / `makemigrations --check --dry-run` | No issues / No changes detected |
| `showmigrations notifications` | `0001` applied; `0002_in_app_notifications` pending on the development database (applied in tests only) |
| `git diff --check` | Clean |

Test count 967 → 1003 (+36). Warnings: only the pre-existing ones. No file in `apps/wallets` or `apps/payments`
changed; producer hooks in memberships, advertising, videos, viewing, rewards and withdrawals only add notice
writes after existing audit events — no financial rule, trigger, constraint or lock changed.

## 10. Limitations

* Notice text is stored in English at creation; it is not translated per reader (Swahili catalogue: CC-030).
* No notification preferences or opt-out (decision D8); no SMS/push (not a recovered requirement).
* No bounce/complaint handling (needs the email provider's webhooks, RD-6).
* Email copies need a configured SMTP provider and a running worker (hosting decisions D3/D4).
* Support-ticket notices arrive with Phase 15-B.
* Browser checks (keyboard, screen reader, 360 px, contrast of the badge in dark mode) are manual (Phase 16).
