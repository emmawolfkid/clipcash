# Phase 15-B — Customer Support

Baseline: `97ada9e` (Phase 15-A, repaired). Status: implemented and tested, **not committed**.

## Scope

Ticket-based customer support for members and advertisers, and a staff support workspace in
the administrator console. No attachments (D14), no SLA timers (D16), no financial changes.

## Data model (`apps/support/models.py`)

| Model | Purpose |
|---|---|
| `Ticket` | Reference `CS-XXXXXXXX` (8 Crockford-style characters, no 0/O/1/I/L), requester, optional advertiser organization, subject (≤150), category, priority, status, assignee, `submission_key`, `last_activity_at`, `resolved_at`, `closed_at`. |
| `TicketMessage` | Conversation entry (≤5000 chars). `internal=True` marks a staff-only note; `from_staff` marks staff replies. |
| `TicketEvent` | History: CREATED, ASSIGNED, STATUS, PRIORITY, REOPENED with from/to values. |

Categories: Account, Membership, Videos, Advertising, Demo (rewards/wallet/withdrawals), Technical, Other.
Priorities: Low, Normal (default), High, Urgent — set by staff only.

Permissions: `support.view_tickets` (read the queue and tickets) and `support.manage_tickets`
(assign, reply, internal notes, status, priority). Superusers hold both implicitly. Neither grants
any financial, wallet, withdrawal, payment, audit or account-administration permission.

## Workflow (`apps/support/services.py`)

```
OPEN ─┬─> IN_PROGRESS ─┬─> WAITING_FOR_CUSTOMER ─┐
      │                │                          │ customer reply → IN_PROGRESS (assigned) / OPEN
      └────────────────┴─> RESOLVED ──> CLOSED (final)
                              │
                              └─ customer reply ≤ 14 days, or staff → reopened
```

* **Create** (customers only: active `MEMBER` accounts; email verification not required).
  Validated subject/description/category; organization must be one the customer is an active
  team member of. Same form submitted twice → same ticket (`submission_key`). 5 tickets/hour.
* **Customer reply**: WAITING → back to the team; RESOLVED → reopened if within
  `SUPPORT_REOPEN_DAYS` (14), otherwise refused ("open a new ticket"); CLOSED → refused.
  Idempotent per submission; 30 replies/hour.
* **Assign / reassign / unassign** (manage permission): assignee must be active staff with
  `support.manage_tickets`. Assigning an OPEN ticket → IN_PROGRESS; unassigning IN_PROGRESS → OPEN.
* **Staff reply** with optional "then" (waiting for customer / resolved); **internal note** never
  changes status and is never shown or notified to the customer.
* **Status change** uses an `expected_status` stale check under `SELECT … FOR UPDATE`; only the
  transitions above are offered and accepted.
* **Priority change** recorded in history.

Customers cannot assign, set priority, write internal notes or change status: the customer form has
no such fields, extra POST fields are ignored, and the services check the actor's role.

## Visibility (decision D15 default)

| Viewer | Sees |
|---|---|
| Requester, personal ticket | Own ticket |
| Requester, organization ticket | Own ticket while still an active team member |
| Organization OWNER | All of that organization's tickets |
| Organization MANAGER | Only their own tickets |
| Anyone else | 404 (not found) |
| Staff with `view_tickets` | Queue and tickets (read only) |
| Staff with `manage_tickets` | Queue, tickets and all actions |

Customers never see internal notes, assignment/priority history or staff email addresses
(staff replies are labelled "ClipCash support").

## Pages

Customer (`/support/`, member layout): ticket list with reference/subject search, status filter and
pagination; "Ask for help" form; ticket detail with conversation and reply form.

Staff (`/manage/support/`, console layout): queue (default: unresolved) with status, category,
priority and assignee (anyone / me / unassigned / agent) filters, reference or subject search and
pagination; ticket page with details, assign, priority, status and reply/internal-note forms.
`console_links()` exposes `can.support` for a navigation entry.

Navigation (approved): the shared member sidebar (`components/member_sidebar.html`, also used by
advertiser pages via `advertising/layout.html` → `members/layout.html`) has a "Support" entry; the
console sidebar has a "Customer support → Support queue" group shown only when `can.support`.

## Notifications (Phase 15-A service, category SUPPORT)

| Event | Recipients | Email |
|---|---|---|
| Ticket created | Requester (confirmation); all support agents | No |
| Assigned | New assignee (not when self-assigned) | No |
| Customer reply | Assignee, or all agents when unassigned | No |
| Reopened by customer | Assignee, or all agents | No |
| Staff public reply | Requester | Yes |
| Resolved | Requester | Yes |
| Closed | Requester | No |

Every notice has a dedupe key per ticket/message/event, so retries produce no duplicates. Notice
texts carry the reference only — never message text or internal notes.

## Audit (category SUPPORT)

`support.ticket.created`, `.customer_replied`, `.staff_replied`, `.note_added`, `.assigned`,
`.status_changed`, `.priority_changed`, and security event `support.action.denied`. Metadata holds
references, statuses and ids only — never message bodies.

## Database integrity (`support/0002_integrity.py`)

* Tickets cannot be deleted; reference, requester, organization, submission key and creation time
  are immutable; tickets are inserted OPEN; the status state machine above is enforced; CLOSED
  tickets cannot change at all.
* Messages and history events are append-only.
* Check constraints: valid status/category/priority, non-blank subject and body, internal notes
  only from staff; unique (requester, submission_key) and (ticket, author, submission_key).

## Migrations

| Migration | Change |
|---|---|
| `support/0001_initial` | Ticket, TicketMessage, TicketEvent, indexes, constraints, permissions |
| `support/0002_integrity` | PostgreSQL triggers (above) |
| `audit/0013_support_category` | Adds SUPPORT to the audit category constraint |
| `notifications/0003_support_category` | Adds SUPPORT to the notification category constraint |

All additive; no financial table is touched. Not yet applied to the local dev database.

## Security

CSRF on every POST; all output auto-escaped (`linebreaksbr` on message bodies); length limits on
every field; rate limits; idempotent submissions; object-level checks on every read and write;
no file uploads; the Django admin registration is read-only.

## Limitations

* No attachments, SLA timers, canned responses or email-in replies.
* Customers may not reopen a CLOSED ticket (by design) — they open a new one.
* Agent notifications for new tickets go to every agent (no routing by category).
* The two sidebar files are shared with the UI redesign (see the Phase 15-B file list).
