# Verified viewing (Phase 06)

Watch sessions for sponsored campaigns. They carry server-checked evidence, a
rule-based anti-fraud layer and staff review.

**A verified view is not a reward.** Nothing in this phase creates money,
balances, ledger entries, wallet credits or payments. `VERIFIED_EVIDENCE`
means *the server's rules passed*. It is not proof that a person watched
attentively.

## What the browser can and cannot prove

Every value the browser sends is untrusted: playback position, play or pause
state, timestamps, and anything a modified page or script could fake. The
player itself, whether Bunny's iframe or a `<video>` element, can be automated.

The server therefore credits only what it can bound:

- **Elapsed time:** credited watch time never exceeds the time the server measured between two heartbeats, plus a small tolerance (2 s). In total it never exceeds the time since the session started.
- **Forward, continuous playback only:**
  - Backward seeks earn nothing.
  - Forward jumps beyond elapsed time + tolerance + 15 s are treated as seeks: no credit, and they're counted.
  - Faster-than-real-time progress is credited at real time at most, and counted as a speed anomaly.
  - Movement while paused is a seek.
  - A heartbeat gap during playback (more than 45 s) earns nothing.
- **Continuous coverage:** a session must reach the required point by unbroken credited playback from the start. Watching the same part twice, or skipping ahead, doesn't count.
- **The server alone decides the outcome.** The protocol has no "completed" field (unknown keys are rejected); `ended` is just an event.

None of this is DRM, tamper-proofing or proof of human attention. A
determined attacker could script real-time playback. Such views pass the
rules, which is why outcomes are labelled as evidence, why repeated problems
force review, and why any future reward step must add its own controls.

## Model: `WatchSession`

The model has three independent status fields:

| Field | Values |
| --- | --- |
| `state` (lifecycle) | CREATED → IN_PROGRESS → FINALIZED; CREATED → EXPIRED; CREATED / IN_PROGRESS → INVALIDATED |
| `outcome` | PENDING → VERIFIED_EVIDENCE · INSUFFICIENT_EVIDENCE · SUSPICIOUS_REVIEW · REJECTED · EXPIRED; SUSPICIOUS_REVIEW → VERIFIED_EVIDENCE / REJECTED (review) |
| `review_status` | NONE → PENDING → CONFIRMED / REJECTED |

What a session records:

- **Identity:** the member, the campaign, the approved campaign revision, the video, the video length and the required watch time. None of these can change (database trigger).
- **Evidence:** a bounded summary only.
  - `credited_ms` and `coverage_ms`;
  - the last sequence number and last position;
  - counters for heartbeats, duplicates, out-of-order events, invalid payloads, events sent too fast, seeks (and milliseconds seeked forward), speed anomalies and heartbeat gaps;
  - `flags` and `reasons`.
  No raw telemetry, IP addresses or device fingerprints are stored.
- **Session ID:** the primary key is a random UUID4. It's useless without the owner's signed-in session, because lookups are always filtered by member, so another member's ID is simply "not found".
- **Database guarantees:**
  - one open session per member and campaign;
  - a unique `qualifying_slot` (1 to `max_views_per_member`) per member and campaign, so a view can never be counted twice;
  - VERIFIED requires credited time and coverage of at least the required time. Reviewers can't fabricate evidence: the database refuses it;
  - evidence only grows while a session is open, and is frozen once it's closed;
  - valid transitions only, and no deletes.

## Starting a session

`services.start_session(member, campaign_id)` runs as one transaction holding a
lock on the member's row:

1. The `videos` feature must be on. Then `apps.advertising.services.campaign_eligibility(campaign, member)` is checked. It covers:
   - the `advertisers` flag, an ACTIVE campaign within its schedule, and an approved advertiser;
   - an authorized, available video;
   - the Phase 03 membership and the video's required entitlement (which also covers the account state and email verification);
   - plan targeting.
2. Members who work for the campaign's advertiser are refused (`own_campaign`).
3. If an open session for the campaign is still valid, it's returned unchanged: double clicks and second tabs get the same session. A stale open session is closed first.
4. A member can start at most `VIEWING_MAX_STARTS_PER_HOUR` sessions (default 20) an hour; beyond that, starts are refused with `too_many_attempts`.
5. Only `VIEWING_MAX_CONCURRENT_SESSIONS` sessions (default 1) may be open at once. Starting another campaign closes, and evaluates, the oldest open session.
6. Capacity counts sessions holding a slot (verified, or under review). At the limit, starts are refused with `limit_reached`.
7. The session is created. It must start playing within 5 minutes, and it expires after video length × 2 + 15 minutes (at most 6 hours).

Denials are audited (`viewing.session.denied`) and shown as plain messages.
Starting a session is never a view.

Playback (`services.playback_for`) re-checks eligibility, then uses the Phase 04
`authorize_playback`: the same short-lived signed Bunny embed URL, or the demo
sample. There is no separate playback path.

## Evidence endpoint

`POST /watch/session/<id>/events/` takes JSON with CSRF protection and a body of at most 1 KB:

```
{"seq": <1..1000000>, "position_ms": <0..length+2s>, "state": "playing"|"paused"|"ended", "client_ts": optional}
```

Each event is processed under a row lock:

- **Ownership:** another member's session returns 404.
- **Closed or expired sessions:** 409.
- **Re-checking eligibility:** if the campaign changed revision or video, was paused or ended, or the advertiser, membership or video stopped qualifying, the session is **INVALIDATED** (outcome REJECTED) and returns 409.
- **Rate limits:** more than one event per second per session, or more than `VIEWING_MAX_EVENTS_PER_MINUTE` per member (shared database counter), returns 429. A session accepts at most 2,000 events.
- **Sequence numbers:** the same sequence number again is an idempotent duplicate. A lower one is out of order and returns 409.
- **Rejected evidence** is counted, never credited. It's audited on the first rejection and every 10th after that, so the audit trail isn't flooded.

`static/js/video-watch.js` sends a heartbeat every 10 seconds while playing,
and on play, pause, seek and end. It supports the demo `<video>` element and
Bunny's player iframe through the Player.js postMessage protocol, accepting
messages only from Bunny's origins. **The Bunny Player.js event integration
must be verified against a real Bunny library** (see the Phase 04 real-Bunny
checklist). In demo mode, set `VIDEO_DEMO_SAMPLE_URL` to a clip you're allowed
to use, and set the demo video's duration to match it. Without a sample file,
demo sessions can't produce evidence.

## Closing and verification

Sessions close in three ways:

- the `ended` event;
- the member pressing "Finish session" (`finish_session`, which is idempotent);
- the `viewing.expire_sessions` job (every 60 s, SKIP LOCKED, idempotent). It expires sessions that were never started, and closes sessions that have been idle for 5 minutes or are past their expiry.

Outcome rules:

| Condition | Outcome |
| --- | --- |
| Never started | EXPIRED |
| Eligibility no longer holds | INVALIDATED / REJECTED, with the reason |
| Video length unknown, or under 5 s | INSUFFICIENT (`duration_unknown`): fails closed |
| `credited_ms` below required | INSUFFICIENT (`watch_time_short`) |
| `coverage_ms` below required | INSUFFICIENT (`coverage_short`) |
| No free slot | REJECTED (`limit_reached`) |
| Fraud flags, or 3 or more rejected/suspicious sessions in 7 days | SUSPICIOUS_REVIEW: the slot is held, staff review is required |
| Otherwise | VERIFIED_EVIDENCE: slot assigned |

The required watch time is the larger of `VIEWING_MIN_WATCH_SECONDS` (15 s) and
`VIEWING_MIN_WATCH_PERCENT` (90 %) of the video length, capped at the full
length. All units are integer milliseconds, with no floating point.

## Fraud rules (reason codes)

| Flag | Rule |
| --- | --- |
| `impossible_speed` | 3 or more speed anomalies |
| `large_seeks` | Forward seeking of more than 10 % of the video (at least 15 s) |
| `replayed_events` | 3 or more out-of-order events, or 10 or more duplicates |
| `excessive_events` | 10 or more events refused for being too frequent |
| `heartbeat_gaps` | 3 or more gaps during playback (long pauses don't count) |
| `invalid_evidence` | 5 or more invalid payloads |
| `rapid_attempts` | Session started after half the hourly start limit |

Flags only send a session to review when the watch thresholds were met;
otherwise the outcome is INSUFFICIENT and the flags are recorded.

**False positives:** slow networks and paused tabs can trip `heartbeat_gaps`
or speed rules. That's why flagged sessions go to a person, never to automatic
rejection.

## Staff review

The console at `/manage/viewing/` requires `viewing.review_viewing`, which belongs to Super Administrators.

- **Session list:** filter by member, campaign, outcome, state, flag, or "awaiting review". It also lists members with repeated problems over the last 30 days.
- **Session detail:** the evidence summary and the audit history.
- **Decisions** need a reason and the status the reviewer saw (stale decisions are refused), and repeating the same decision does nothing. Reviewers can't review their own viewing.
  - **Confirm** keeps the server's evidence: the database refuses VERIFIED without it.
  - **Reject** releases the slot, so the member can earn the view honestly again.
- No decision creates a reward.

## Audit (category `VIEWING`)

| Event | Kind |
| --- | --- |
| `viewing.session.started` | business |
| `viewing.session.denied` | security |
| `viewing.session.expired` | business |
| `viewing.session.invalidated` | business |
| `viewing.session.finalized` | business |
| `viewing.evidence.rejected` | security (sampled) |
| `viewing.fraud.flagged` | security |
| `viewing.review.decided` | business |

Metadata holds IDs, reason codes and whole seconds only. It never contains
playback URLs, tokens or telemetry.

**Retention:** sessions are permanent records, because they're the evidence
for later decisions. Audit events follow the Phase 02B retention commands.

## Configuration

All settings are in `config/settings.py`, using whole numbers; environment overrides start with `DJANGO_VIEWING_`. They're validated by `viewing.E001` and `viewing.E002`.

| Setting | Default | Allowed range |
| --- | --- | --- |
| `VIEWING_MIN_WATCH_PERCENT` | 90 | 50–100 |
| `VIEWING_MIN_WATCH_SECONDS` | 15 | 5–600 |
| `VIEWING_HEARTBEAT_SECONDS` | 10 | 2–30 |
| `VIEWING_MAX_HEARTBEAT_GAP_SECONDS` | 45 | 10–120; at least 2 × the heartbeat |
| `VIEWING_MAX_POSITION_JUMP_SECONDS` | 15 | 1–60 |
| `VIEWING_IDLE_SECONDS` | 300 | 60–3600 |
| `VIEWING_MAX_CONCURRENT_SESSIONS` | 1 | 1–3 |
| `VIEWING_MAX_STARTS_PER_HOUR` | 20 | 1–200 |
| `VIEWING_MAX_EVENTS_PER_MINUTE` | 30 | 6–120 |

Clients never send or influence thresholds.

## Phase 07 contract (not implemented)

- `services.verified_views(campaign)` is a read-only queryset of finalized VERIFIED_EVIDENCE sessions holding a slot. A view appears there at most once per slot.
- A future rewards engine must, inside its own audited transaction:
  1. require the campaign to be financially eligible (`Campaign.financially_eligible`: funding VERIFIED from reconciled advertiser money, Phase 08, and the locked `real_money_rewards` flag on);
  2. pay only from that campaign's restricted funding (ledger `CAMPAIGN_RESTRICTED_FUNDING`), never from membership revenue;
  3. record a one-to-one link from each reward to its session, enforced unique;
  4. honour later review reversals.
- Phase 06 never calls into rewards, wallets or the ledger.
