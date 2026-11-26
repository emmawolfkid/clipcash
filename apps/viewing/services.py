"""
Verified viewing services (Phase 06): the only way to create, advance, close
and review watch sessions.

Trust model
-----------
Everything the browser sends is untrusted. The server:
* decides eligibility when a session starts (account, membership, campaign,
  targeting, advertiser, video, approved revision, views_per_member) and
  re-checks it on every piece of evidence;
* credits watch time only for continuous forward playback, and never more
  than the server-measured time between two heartbeats (plus a small
  tolerance), so seeking, speed-ups, gaps and replays earn nothing;
* alone decides the outcome when the session closes. A browser "completed"
  claim does not exist in the protocol.

VERIFIED_EVIDENCE means "the server rules passed". It is NOT proof that a
person watched attentively, and it is NOT a reward: nothing here touches
wallets, the ledger or payments (see docs/architecture/viewing.md).
"""

import hashlib
from dataclasses import dataclass
from datetime import timedelta

import django.dispatch
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.utils.translation import gettext_noop as N_

from apps.advertising import services as advertising
from apps.advertising.models import AdvertiserMembership, AdvertiserVideo, Campaign, Organization
from apps.audit.services import DENIED, FAILURE, record_business_event, record_security_event
from apps.core import features, ratelimit
from apps.memberships.access import check_access
from apps.videos.models import Video
from apps.rewards.terms import PREFETCH as TERMS_PREFETCH
from apps.rewards.terms import plan_allowed
from apps.videos.services import authorize_playback

from .models import OPEN_STATES, WatchSession

St = WatchSession.State
Out = WatchSession.Outcome
Rev = WatchSession.Review

MAX_SESSION_SECONDS = 6 * 3600
MAX_SEQUENCE = 1_000_000
EVENT_STATES = ("playing", "paused", "ended")
EVENT_KEYS = {"seq", "position_ms", "state", "client_ts"}
MIN_REVIEW_REASON = 5

# Sent INSIDE the transaction that rejects or revokes a session, so dependent
# records (Phase 07 demo rewards) are reversed atomically with it.
session_reversed = django.dispatch.Signal()
# FIN-01: sent (inside the closing transaction) whenever a session's outcome is decided or changed by
# a review: closing, expiry, invalidation, review confirmation or rejection. Receivers settle what the
# session holds (apps.earnings releases or issues its reserved real reward).
session_closed = django.dispatch.Signal()


class ViewingError(Exception):
    code = "viewing_error"
    http_status = 400

    def __init__(self, message, *, code=None, http_status=None):
        super().__init__(message)
        if code:
            self.code = code
        if http_status:
            self.http_status = http_status


class NotFound(ViewingError):
    code = "not_found"
    http_status = 404


class Denied(ViewingError):
    code = "denied"
    http_status = 403


class SessionClosed(ViewingError):
    code = "session_closed"
    http_status = 409


class EvidenceRejected(ViewingError):
    code = "evidence_rejected"


class InvalidState(ViewingError):
    code = "invalid_state"


class StaleDecision(ViewingError):
    code = "stale"
    http_status = 409


DENIAL_MESSAGES = {
    "not_authenticated": gettext_lazy("Please sign in to watch sponsored videos."),
    "account_inactive": gettext_lazy("Your account cannot watch sponsored videos at the moment."),
    "email_unverified": gettext_lazy("Please confirm your email address first."),
    "no_membership": gettext_lazy("An active membership is needed to watch sponsored videos."),
    "membership_suspended": gettext_lazy("Your membership is suspended."),
    "entitlement_missing": gettext_lazy("Your membership plan does not include this video."),
    "plan_not_targeted": gettext_lazy("This campaign is for members of other plans."),
    "limit_reached": gettext_lazy("You have already watched this campaign the maximum number of times."),
    "daily_limit": gettext_lazy("You already have a verified view of this campaign today. Come back tomorrow."),
    "too_many_attempts": gettext_lazy("Too many attempts. Please try again later."),
    "own_campaign": gettext_lazy("You cannot watch campaigns of an advertiser you work for."),
    "feature_disabled": gettext_lazy("Sponsored videos are not available at the moment."),
    # FIN-01: real TZS offers are reserved at start; these explain why one could not be.
    "earning_amount_limit": gettext_lazy("You have reached today's earning limit. Come back tomorrow."),
    "earning_view_limit": gettext_lazy("You have reached today's limit of rewarded views. Come back tomorrow."),
    "reward_funding_unavailable": gettext_lazy("This campaign's reward funding is used up for now."),
}


def denial_message(reason):
    return DENIAL_MESSAGES.get(reason, _("This campaign is not available to you right now."))


# --- Thresholds ------------------------------------------------------------------------


def required_ms_for(duration_seconds):
    """Required credited watch time in ms, or None (fail closed) for unknown/too-short videos."""
    if not isinstance(duration_seconds, int) or duration_seconds < settings.VIEWING_MIN_VIDEO_SECONDS:
        return None
    total = duration_seconds * 1000
    by_percent = (total * settings.VIEWING_MIN_WATCH_PERCENT + 99) // 100
    return min(total, max(settings.VIEWING_MIN_WATCH_SECONDS * 1000, by_percent))


def _ms(delta):
    return max(0, int(delta.total_seconds() * 1000))


# --- Permissions ------------------------------------------------------------------------


def can_review_viewing(user):
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.is_active
                and user.is_staff and user.has_perm("viewing.review_viewing"))


def _works_for(member, organization_id):
    return AdvertiserMembership.objects.filter(user=member, organization_id=organization_id, is_active=True).exists()


# --- Starting a session -------------------------------------------------------------------


def _expiry_for(now, duration_seconds):
    if duration_seconds:
        seconds = duration_seconds * 2 + settings.VIEWING_SESSION_GRACE_SECONDS
    else:
        seconds = 3600
    return now + timedelta(seconds=min(MAX_SESSION_SECONDS, seconds))


def start_session(member, campaign_id):
    """
    Create (or resume) the member's watch session for a campaign. Starting is
    not a view. Denials raise Denied(code=reason) and are audited.
    """
    campaign = Campaign.objects.filter(pk=campaign_id).first()
    if campaign is None:
        raise NotFound(_("Campaign not found."))
    result, reason, flags = _start_locked(member, campaign)
    if reason:
        record_security_event("viewing.session.denied", outcome=DENIED,
                              actor=member if getattr(member, "is_authenticated", False) else None,
                              target=campaign, metadata={"campaign": str(campaign.pk), "reason": reason})
        raise Denied(denial_message(reason), code=reason)
    return result


def _start_locked(member, campaign):
    """(session, None, flags) or (None, reason, None). Holds the member row lock."""
    if not getattr(member, "is_authenticated", False):
        return None, "not_authenticated", None
    now = timezone.now()
    with transaction.atomic():
        # One lock per member serializes starts, limits and concurrent tabs.
        get_user_model().objects.select_for_update().get(pk=member.pk)
        campaign = (Campaign.objects.select_related("organization", "video", "video__category")
                    .get(pk=campaign.pk))
        if not features.is_enabled("videos"):
            return None, "feature_disabled", None
        decision = advertising.campaign_eligibility(campaign, member, now)
        if not decision:
            return None, decision.reason, None
        if _works_for(member, campaign.organization_id):
            return None, "own_campaign", None

        mine = WatchSession.objects.select_for_update().filter(member=member)
        existing = mine.filter(campaign=campaign, state__in=OPEN_STATES).first()
        if existing is not None:
            if existing.campaign_revision == campaign.revision and now < existing.expires_at and (
                    existing.state == St.IN_PROGRESS or now < existing.start_deadline):
                return existing, None, None  # Duplicate start / second tab: same session.
            _close(existing, now, trigger="superseded")

        recent = mine.filter(created_at__gte=now - timedelta(hours=1)).count()
        if recent >= settings.VIEWING_MAX_STARTS_PER_HOUR:
            return None, "too_many_attempts", None

        others = list(mine.filter(state__in=OPEN_STATES).exclude(campaign=campaign).order_by("created_at"))
        while len(others) >= settings.VIEWING_MAX_CONCURRENT_SESSIONS:
            _close(others.pop(0), now, trigger="superseded")  # Starting another video ends the oldest.

        used = mine.filter(campaign=campaign, qualifying_slot__isnull=False).count()
        if used >= campaign.max_views_per_member:
            return None, "limit_reached", None
        if verified_today(member, campaign, now, sessions=mine):
            return None, "daily_limit", None  # CR-01: one credited view per campaign per day.

        from apps.earnings import services as earnings
        from apps.rewards.services import current_offer

        quoted_policy, quoted_credits = current_offer(campaign, now)  # CR-01: fixed for this session.
        # FIN-01: a real TZS offer is reserved now (funding and daily limits, under locks) or refused.
        tzs_offer = earnings.quote_for_start(member, campaign, now)
        if tzs_offer.reason:
            return None, tzs_offer.reason, None

        flags = ["rapid_attempts"] if recent >= max(3, settings.VIEWING_MAX_STARTS_PER_HOUR // 2) else []
        duration = campaign.video.duration_seconds
        session = WatchSession.objects.create(
            member=member, campaign=campaign, campaign_revision=campaign.revision, video=campaign.video,
            start_deadline=now + timedelta(seconds=settings.VIEWING_START_WINDOW_SECONDS),
            expires_at=_expiry_for(now, duration),
            video_duration_ms=duration * 1000 if duration else None, required_ms=required_ms_for(duration),
            flags=flags, quoted_policy=quoted_policy, quoted_credits=quoted_credits)
        if tzs_offer.is_tzs:
            earnings.reserve(session, tzs_offer, now)
        record_business_event("viewing.session.started", actor=member, target=session,
                              metadata={"campaign": str(campaign.pk), "revision": campaign.revision,
                                        "quoted_credits": quoted_credits, "quoted_tzs": tzs_offer.amount})
    return session, None, flags


def local_day_bounds(now):
    """[start, end) of the local calendar day (settings.TIME_ZONE) containing ``now``."""
    local = timezone.localtime(now)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def verified_today(member, campaign, now, sessions=None):
    """CR-01 same-day rule: does the member already have a counted (verified or under review)
    view of this campaign that closed today, local time?"""
    start, end = local_day_bounds(now)
    sessions = sessions if sessions is not None else WatchSession.objects.filter(member=member)
    return sessions.filter(campaign=campaign, qualifying_slot__isnull=False,
                           finalized_at__gte=start, finalized_at__lt=end).exists()


def session_for(member, session_id):
    """The member's own session, or NotFound (other members' ids look nonexistent)."""
    session = (WatchSession.objects.select_related("campaign", "campaign__organization", "video",
                                                   "video__category")
               .filter(pk=session_id, member=member).first())
    if session is None:
        raise NotFound(_("Session not found."))
    return session


# --- Revocation ------------------------------------------------------------------------------


def _revocation_reason(session, now):
    """Why an open session may no longer continue ("" = it may)."""
    campaign = (Campaign.objects.select_related("organization", "video", "video__category")
                .get(pk=session.campaign_id))
    if (campaign.revision != session.campaign_revision or campaign.approved_revision != session.campaign_revision
            or campaign.video_id != session.video_id):
        return "campaign_changed"
    decision = advertising.campaign_eligibility(campaign, session.member, now)
    if not decision:
        return decision.reason
    return ""


def playback_for(member, session_id):
    """Signed Phase 04 playback for an open session the member owns: (session, reason, playback)."""
    session = session_for(member, session_id)
    if not session.is_open:
        return session, "session_closed", None
    now = timezone.now()
    reason = _revocation_reason(session, now)
    if reason:
        with transaction.atomic():
            locked = WatchSession.objects.select_for_update().get(pk=session.pk)
            if locked.is_open:
                _invalidate(locked, now, reason)
        session.refresh_from_db()
        return session, reason, None
    reason, playback = authorize_playback(member, session.video)  # Same signed URLs as the library.
    if reason != "ok" or playback is None:
        return session, reason, None
    return session, "ok", playback


# --- Evidence -------------------------------------------------------------------------------


@dataclass
class EventResult:
    accepted: bool
    code: str
    session: WatchSession
    http_status: int = 200


def _parse_event(payload, duration_ms):
    if not isinstance(payload, dict) or not set(payload) <= EVENT_KEYS:
        raise EvidenceRejected("Invalid evidence.", code="invalid_payload")
    seq, position, state = payload.get("seq"), payload.get("position_ms"), payload.get("state")
    for value in (seq, position):
        if type(value) is not int:
            raise EvidenceRejected("Invalid evidence.", code="invalid_payload")
    if not 1 <= seq <= MAX_SEQUENCE or state not in EVENT_STATES:
        raise EvidenceRejected("Invalid evidence.", code="invalid_payload")
    limit = (duration_ms + settings.VIEWING_POSITION_TOLERANCE_MS) if duration_ms else MAX_SESSION_SECONDS * 1000
    if not 0 <= position <= limit:
        raise EvidenceRejected("Invalid playback position.", code="invalid_position")
    return seq, position, state


def _throttle_key(member):
    return hashlib.sha256(f"viewing-events:{member.pk}".encode()).hexdigest()


def _is_terminal(payload):
    """The player's final "ended" event (validated later like any other event)."""
    return isinstance(payload, dict) and payload.get("state") == "ended"


def record_event(member, session_id, payload):
    """Apply one heartbeat. Returns EventResult; rejected evidence is counted, never credited.

    UI-DASH-03: the 1-second minimum interval between events stops floods of heartbeats. The
    terminal "ended" event is exempt from that interval only: players send it the moment the video
    finishes, often within a second of the last heartbeat, and refusing it left the session open.
    It is still subject to the per-minute and per-session limits and to every evidence rule in
    _apply_event (sequence, duplicates, position, speed, credit capped at server time), and it
    closes the session, so the exemption can be used once per session at most."""
    over_limit = ratelimit.hit(_throttle_key(member), 60) > settings.VIEWING_MAX_EVENTS_PER_MINUTE
    now = timezone.now()
    audit = None
    with transaction.atomic():
        session = (WatchSession.objects.select_for_update().select_related("member")
                   .filter(pk=session_id, member=member).first())
        if session is None:
            raise NotFound(_("Session not found."))
        if not session.is_open:
            return EventResult(False, "session_closed", session, 409)
        if now >= session.expires_at:
            _close(session, now, trigger="expired")
            return EventResult(False, "session_closed", session, 409)
        reason = _revocation_reason(session, now)
        if reason:
            _invalidate(session, now, reason)
            return EventResult(False, reason, session, 409)

        too_soon = bool(session.last_event_at and _ms(now - session.last_event_at)
                        < settings.VIEWING_MIN_EVENT_INTERVAL_MS)
        if over_limit or (too_soon and not _is_terminal(payload)):
            session.rate_rejection_count += 1
            session.save(update_fields=["rate_rejection_count", "updated_at"])
            audit = _rejection_audit(session, "rate_limited", session.rate_rejection_count)
            result = EventResult(False, "rate_limited", session, 429)
        elif session.event_count >= settings.VIEWING_MAX_EVENTS_PER_SESSION:
            session.rejected_event_count += 1
            session.save(update_fields=["rejected_event_count", "updated_at"])
            result = EventResult(False, "too_many_events", session, 429)
        else:
            result, audit = _apply_event(session, payload, now)
    if audit:
        record_security_event("viewing.evidence.rejected", outcome=FAILURE, actor=member, target=result.session,
                              metadata=audit)
    return result


def _rejection_audit(session, reason, count):
    # Audit the 1st and every 10th rejection of a kind: visible, but no flood.
    if count == 1 or count % 10 == 0:
        return {"campaign": str(session.campaign_id), "reason": reason, "count": count}
    return None


def _apply_event(session, payload, now):
    try:
        seq, position, state = _parse_event(payload, session.video_duration_ms)
    except EvidenceRejected as error:
        session.rejected_event_count += 1
        session.save(update_fields=["rejected_event_count", "updated_at"])
        return EventResult(False, error.code, session, 400), _rejection_audit(session, error.code,
                                                                              session.rejected_event_count)
    if seq == session.last_sequence:
        session.duplicate_event_count += 1  # Retried request: idempotent, nothing credited.
        session.save(update_fields=["duplicate_event_count", "updated_at"])
        return EventResult(True, "duplicate", session), None
    if seq < session.last_sequence:
        session.out_of_order_count += 1
        session.save(update_fields=["out_of_order_count", "updated_at"])
        return EventResult(False, "out_of_order", session, 409), _rejection_audit(session, "out_of_order",
                                                                                 session.out_of_order_count)
    tolerance = settings.VIEWING_POSITION_TOLERANCE_MS
    if session.state == St.CREATED:
        session.state = St.IN_PROGRESS
        session.started_at = now
        if position <= tolerance:
            session.coverage_ms = position
    else:
        elapsed = _ms(now - session.last_event_at)
        moved = position - session.last_position_ms
        credit = 0
        if elapsed > settings.VIEWING_MAX_HEARTBEAT_GAP_SECONDS * 1000:
            if session.last_state_playing:
                session.gap_count += 1  # Unobserved playing time is never credited.
            # A long pause is normal: nothing credited, nothing flagged.
        elif moved < -tolerance:
            session.seek_count += 1  # Backward seek: allowed, earns nothing.
        elif moved > elapsed + tolerance + settings.VIEWING_MAX_POSITION_JUMP_SECONDS * 1000:
            session.seek_count += 1
            session.seek_forward_ms = min(session.seek_forward_ms + moved, 2**31 - 1)
        elif not session.last_state_playing:
            if moved > tolerance:
                session.seek_count += 1  # Position moved while paused.
        elif moved > elapsed + tolerance:
            session.speed_anomaly_count += 1
            credit = elapsed  # Faster than real time: credit real time at most.
        else:
            credit = max(0, moved)
        if credit:
            ceiling = _ms(now - session.started_at)  # Never more than server-observed time.
            session.credited_ms = min(session.credited_ms + credit, ceiling)
            if session.last_position_ms <= session.coverage_ms + tolerance:
                session.coverage_ms = max(session.coverage_ms, position)
    session.last_sequence = seq
    session.last_position_ms = position
    session.last_state_playing = state == "playing"
    session.last_event_at = now
    session.event_count += 1
    session.save()
    if state == "ended":
        _close(session, now, trigger="ended")
    return EventResult(True, "ok", session), None


# --- Closing ---------------------------------------------------------------------------------


def fraud_flags(session):
    """Rule-based reason codes (see docs/architecture/viewing.md)."""
    flags = set(session.flags or [])
    duration = session.video_duration_ms or 0
    if session.speed_anomaly_count >= 3:
        flags.add("impossible_speed")
    if session.seek_forward_ms > max(15_000, duration // 10):
        flags.add("large_seeks")
    if session.out_of_order_count >= 3 or session.duplicate_event_count >= 10:
        flags.add("replayed_events")
    if session.rate_rejection_count >= 10:
        flags.add("excessive_events")
    if session.gap_count >= 3:
        flags.add("heartbeat_gaps")
    if session.rejected_event_count >= 5:
        flags.add("invalid_evidence")
    return sorted(flags)


def _recent_problem_count(session):
    since = timezone.now() - timedelta(days=7)
    return (WatchSession.objects.filter(member_id=session.member_id, finalized_at__gte=since,
                                        outcome__in=[Out.REJECTED, Out.SUSPICIOUS_REVIEW])
            .exclude(pk=session.pk).count())


def _next_slot(session):
    limit = (Campaign.objects.only("max_views_per_member").get(pk=session.campaign_id).max_views_per_member)
    used = set(WatchSession.objects.filter(member_id=session.member_id, campaign_id=session.campaign_id,
                                           qualifying_slot__isnull=False).values_list("qualifying_slot", flat=True))
    free = [slot for slot in range(1, limit + 1) if slot not in used]
    return free[0] if free else None


def _invalidate(session, now, reason):
    session.state = St.INVALIDATED
    session.outcome = Out.REJECTED
    session.reasons = [reason]
    session.flags = fraud_flags(session)
    session.finalized_at = now
    session.save()
    record_business_event("viewing.session.invalidated", actor=None, target=session,
                          metadata={"campaign": str(session.campaign_id), "reason": reason})


def _close(session, now, *, trigger):
    """Close a locked open session and decide its outcome (server rules only). FIN-01: receivers of
    session_closed settle what the session holds (after this transaction commits)."""
    _decide_close(session, now, trigger=trigger)
    session_closed.send(sender=WatchSession, session=session)
    return session


def _decide_close(session, now, *, trigger):
    if session.state == St.CREATED:
        session.state = St.EXPIRED
        session.outcome = Out.EXPIRED
        session.reasons = ["not_started"]
        session.finalized_at = now
        session.save()
        record_business_event("viewing.session.expired", actor=None, target=session,
                              metadata={"campaign": str(session.campaign_id)})
        return session
    reason = _revocation_reason(session, now)
    if reason:
        _invalidate(session, now, reason)
        return session
    flags = fraud_flags(session)
    reasons = []
    session.state = St.FINALIZED
    if session.required_ms is None:
        session.outcome, reasons = Out.INSUFFICIENT_EVIDENCE, ["duration_unknown"]
    elif session.credited_ms < session.required_ms:
        session.outcome, reasons = Out.INSUFFICIENT_EVIDENCE, ["watch_time_short"]
    elif session.coverage_ms < session.required_ms:
        session.outcome, reasons = Out.INSUFFICIENT_EVIDENCE, ["coverage_short"]
    else:
        slot = _next_slot(session)
        if slot is None:
            session.outcome, reasons = Out.REJECTED, ["limit_reached"]
        else:
            session.qualifying_slot = slot
            history = _recent_problem_count(session) >= settings.VIEWING_REVIEW_AFTER_REJECTIONS
            if flags or history:
                session.outcome, session.review_status = Out.SUSPICIOUS_REVIEW, Rev.PENDING
                reasons = ["review_required"] + (["recent_rejections"] if history else [])
            else:
                session.outcome, reasons = Out.VERIFIED_EVIDENCE, ["thresholds_met"]
    session.reasons = reasons
    session.flags = flags
    session.finalized_at = now
    session.save()
    record_business_event("viewing.session.finalized", actor=None, target=session, metadata={
        "campaign": str(session.campaign_id), "outcome": session.outcome, "reasons": reasons,
        "credited_seconds": session.credited_ms // 1000,
        "required_seconds": (session.required_ms or 0) // 1000, "trigger": trigger})
    if flags:
        record_security_event("viewing.fraud.flagged", outcome=FAILURE, actor=None, target=session,
                              metadata={"campaign": str(session.campaign_id), "flags": flags})
    if session.outcome == Out.SUSPICIOUS_REVIEW:
        _notify_reviewers(now)
    return session


# --- Notifications (Phase 15-A) ----------------------------------------------------------------
# Members learn the outcome of a staff decision about their view; the texts never mention
# fraud signals, flags or reviewer notes. Reviewers get at most one queue reminder a day.

MEMBER_NOTICES = {
    "review_confirmed": (N_("Your view was confirmed"), N_("Staff confirmed your view of “{name}”.")),
    "review_rejected": (N_("Your view was not accepted"), N_("After review, your view of “{name}” was not accepted.")),
    "revoked": (N_("A verified view was withdrawn"), N_("Your verified view of “{name}” no longer counts.")),
}


def _notify_member(session, event):
    from apps.notifications import inbox

    title, message = MEMBER_NOTICES[event]
    inbox.notify(session.member, f"viewing.{event}", category=inbox.Category.VIEWING, title=title,
                 message=message, params={"name": session.campaign.name}, link=inbox.link_to("viewing:history"),
                 dedupe_key=f"viewing.{event}:{session.pk}")


def _notify_reviewers(now):
    from apps.notifications import inbox

    inbox.notify(inbox.staff_with_permission("viewing.review_viewing"), "viewing.review_needed",
                 category=inbox.Category.VIEWING, title=N_("Viewing sessions are awaiting review"),
                 message=N_("At least one sponsored view was flagged for a staff decision."), params={},
                 link=inbox.link_to("administration:viewing"), dedupe_key=f"viewing.review_needed:{now.date()}")


def finish_session(member, session_id):
    """Member says playback is over. Idempotent: a closed session is returned as is."""
    with transaction.atomic():
        session = WatchSession.objects.select_for_update().filter(pk=session_id, member=member).first()
        if session is None:
            raise NotFound(_("Session not found."))
        if session.is_open:
            _close(session, timezone.now(), trigger="member")
    return session


def expire_sessions(now=None, limit=500):
    """Background job: expire unstarted sessions and close idle or overdue ones.
    Idempotent and safe to run concurrently (SKIP LOCKED)."""
    now = now or timezone.now()
    counts = {"expired": 0, "closed": 0}
    idle_before = now - timedelta(seconds=settings.VIEWING_IDLE_SECONDS)
    with transaction.atomic():
        due = (WatchSession.objects.select_for_update(skip_locked=True)
               .filter(Q(state=St.CREATED, start_deadline__lte=now)
                       | Q(state=St.IN_PROGRESS) & (Q(expires_at__lte=now) | Q(last_event_at__lte=idle_before)))
               .order_by("created_at")[:limit])
        for session in due:
            was_created = session.state == St.CREATED
            _close(session, now, trigger="idle")
            counts["expired" if was_created else "closed"] += 1
    return counts


# --- Review ------------------------------------------------------------------------------------


def review_session(actor, session, decision, *, expected_status, reason):
    """Staff decision on a suspicious session. ``confirm`` keeps the server's evidence (the
    database refuses VERIFIED without it); ``reject`` releases the counted view."""
    if decision not in ("confirm", "reject"):
        raise InvalidState(_("Unknown decision."))
    if not can_review_viewing(actor):
        record_security_event("viewing.session.denied", outcome=DENIED,
                              actor=actor if getattr(actor, "is_authenticated", False) else None,
                              target=session, metadata={"campaign": str(session.campaign_id),
                                                        "reason": "review_permission_missing"})
        raise Denied("You are not allowed to review viewing sessions.")
    if session.member_id == actor.pk:
        raise Denied("You cannot review your own viewing.", code="self_review")
    reason = (reason or "").strip()
    if len(reason) < MIN_REVIEW_REASON:
        raise ValidationError({"reason": _("Please give a reason (at least 5 characters).")})
    target = Rev.CONFIRMED if decision == "confirm" else Rev.REJECTED
    with transaction.atomic():
        session = WatchSession.objects.select_for_update().get(pk=session.pk)
        if session.review_status == target and expected_status == target:
            return session  # Same decision submitted twice.
        if session.review_status != expected_status:
            raise StaleDecision(_("This session was reviewed or changed since you opened it."))
        if session.review_status != Rev.PENDING:
            raise InvalidState(_("Only sessions awaiting review can be decided."))
        session.review_status = target
        session.reviewed_by = actor
        session.reviewed_at = timezone.now()
        session.review_reason = reason[:300]
        if decision == "confirm":
            session.outcome = Out.VERIFIED_EVIDENCE
        else:
            session.outcome = Out.REJECTED
            session.qualifying_slot = None
        session.save()
        record_business_event("viewing.review.decided", actor=actor, target=session,
                              metadata={"decision": decision, "reason": reason[:100], "flags": session.flags})
        _notify_member(session, "review_confirmed" if session.outcome == Out.VERIFIED_EVIDENCE else "review_rejected")
        if decision == "reject":
            session_reversed.send(sender=WatchSession, session=session, actor=actor, reason=reason[:300])
        session_closed.send(sender=WatchSession, session=session)
    return session


def revoke_verified_session(actor, session, *, reason):
    """Staff revocation of an already VERIFIED view (e.g. fraud found later). The evidence
    stays frozen; outcome and review become REJECTED and the counted view is released.
    Dependent demo rewards are reversed in the same transaction. Idempotent."""
    if not can_review_viewing(actor):
        record_security_event("viewing.session.denied", outcome=DENIED,
                              actor=actor if getattr(actor, "is_authenticated", False) else None,
                              target=session, metadata={"campaign": str(session.campaign_id),
                                                        "reason": "review_permission_missing"})
        raise Denied("You are not allowed to review viewing sessions.")
    if session.member_id == actor.pk:
        raise Denied("You cannot review your own viewing.", code="self_review")
    reason = (reason or "").strip()
    if len(reason) < MIN_REVIEW_REASON:
        raise ValidationError({"reason": _("Please give a reason (at least 5 characters).")})
    with transaction.atomic():
        session = WatchSession.objects.select_for_update().get(pk=session.pk)
        if session.outcome == Out.REJECTED and session.review_status == Rev.REJECTED:
            return session  # Already revoked or rejected.
        if session.outcome != Out.VERIFIED_EVIDENCE:
            raise InvalidState(_("Only verified views can be revoked."))
        session.outcome = Out.REJECTED
        session.review_status = Rev.REJECTED
        session.reviewed_by = actor
        session.reviewed_at = timezone.now()
        session.review_reason = reason[:300]
        session.qualifying_slot = None
        session.save()
        record_business_event("viewing.session.revoked", actor=actor, target=session,
                              metadata={"campaign": str(session.campaign_id), "reason": reason[:100]})
        _notify_member(session, "revoked")
        session_reversed.send(sender=WatchSession, session=session, actor=actor, reason=reason[:300])
    return session


# --- Member listings ------------------------------------------------------------------------------


def available_campaigns(member, now=None, limit=50):
    """[(campaign, views_left, open_session)] the member may start now. Bulk pre-filter
    (no N+1); start_session() re-checks everything authoritatively."""
    now = now or timezone.now()
    if not getattr(member, "is_authenticated", False) or not features.is_enabled("advertisers"):
        return []
    grants = AdvertiserVideo.objects.filter(organization=OuterRef("organization"), video=OuterRef("video"),
                                            revoked_at__isnull=True)
    own = AdvertiserMembership.objects.filter(user=member, is_active=True).values("organization_id")
    candidates = list(
        Campaign.objects.select_related("organization", "video", "video__category").prefetch_related(*TERMS_PREFETCH)
        .filter(status=Campaign.Status.ACTIVE, starts_at__lte=now, ends_at__gt=now,
                organization__verification_status=Organization.Verification.APPROVED,
                video__publication_status=Video.Publication.PUBLISHED, video__processing_status=Video.Processing.READY)
        .filter(Exists(grants)).exclude(organization_id__in=own).order_by("ends_at")[:limit])
    access = {}
    used = dict(WatchSession.objects.filter(member=member, qualifying_slot__isnull=False)
                .values_list("campaign_id").annotate(n=Count("id")))
    open_sessions = {s.campaign_id: s for s in WatchSession.objects.filter(member=member, state__in=OPEN_STATES)}
    rows = []
    for campaign in candidates:
        if not advertising.video_available(campaign.video):
            continue
        entitlement = campaign.video.required_entitlement
        if entitlement not in access:
            access[entitlement] = check_access(member, entitlement=entitlement, now=now)
        decision = access[entitlement]
        if not decision:
            continue
        if not plan_allowed(campaign, decision.subscription.plan_id):
            continue
        left = campaign.max_views_per_member - used.get(campaign.pk, 0)
        if left > 0 or campaign.pk in open_sessions:
            rows.append((campaign, max(0, left), open_sessions.get(campaign.pk)))
    return rows


def with_thumbnails(offers):
    """UI-DASH-02: signed, short-lived thumbnails (the video library's helper) for offers the member
    can watch now. Plan-locked offers keep a placeholder, like locked library videos."""
    from apps.videos.services import thumbnail_url

    for offer in offers:
        offer["thumbnail"] = "" if offer["locked"] else thumbnail_url(offer["campaign"].video)
    return offers


def campaign_offers(member, now=None, limit=50):
    """CR-01: sponsored campaigns as the member sees them before playback.

    [{"campaign", "credits", "left", "open_session", "locked", "reason", "plans", "today_done"}].
    ``locked`` rows are campaigns the member could see but not watch for credits because of
    their plan (or no membership): ``plans`` lists the configured plans that qualify. Server
    checks still run on start (start_session)."""
    from apps.earnings import services as earnings
    from apps.memberships.models import MembershipPlan
    from apps.rewards.services import current_offer
    from apps.rewards.terms import eligible_plan_ids

    now = now or timezone.now()
    if not getattr(member, "is_authenticated", False) or not features.is_enabled("advertisers"):
        return []
    grants = AdvertiserVideo.objects.filter(organization=OuterRef("organization"), video=OuterRef("video"),
                                            revoked_at__isnull=True)
    own = AdvertiserMembership.objects.filter(user=member, is_active=True).values("organization_id")
    candidates = list(
        Campaign.objects.select_related("organization", "video", "video__category").prefetch_related(*TERMS_PREFETCH)
        .filter(status=Campaign.Status.ACTIVE, starts_at__lte=now, ends_at__gt=now,
                organization__verification_status=Organization.Verification.APPROVED,
                video__publication_status=Video.Publication.PUBLISHED, video__processing_status=Video.Processing.READY)
        .filter(Exists(grants)).exclude(organization_id__in=own).order_by("ends_at")[:limit])
    used = dict(WatchSession.objects.filter(member=member, qualifying_slot__isnull=False)
                .values_list("campaign_id").annotate(n=Count("id")))
    start, end = local_day_bounds(now)
    today = set(WatchSession.objects.filter(member=member, qualifying_slot__isnull=False, finalized_at__gte=start,
                                            finalized_at__lt=end).values_list("campaign_id", flat=True))
    open_sessions = {s.campaign_id: s for s in WatchSession.objects.filter(member=member, state__in=OPEN_STATES)}
    plan_names = dict(MembershipPlan.objects.values_list("pk", "name"))
    access, rows = {}, []
    for campaign in candidates:
        if not advertising.video_available(campaign.video):
            continue
        entitlement = campaign.video.required_entitlement
        if entitlement not in access:
            access[entitlement] = check_access(member, entitlement=entitlement, now=now)
        decision = access[entitlement]
        plan_ids = eligible_plan_ids(campaign)
        locked, reason = False, "ok"
        if not decision:
            if decision.reason not in ("no_membership", "entitlement_missing"):
                continue  # Account problems are explained elsewhere, not per campaign.
            locked, reason = True, decision.reason
        elif plan_ids and decision.subscription.plan_id not in plan_ids:
            locked, reason = True, "plan_not_targeted"
        left = max(0, campaign.max_views_per_member - used.get(campaign.pk, 0))
        if not locked and left == 0 and campaign.pk not in open_sessions:
            continue  # Fully used: nothing left to offer.
        _policy, credits = current_offer(campaign, now)
        tzs = earnings.preview_offer(member, campaign, now) if not locked else earnings.Offer()
        rows.append({"campaign": campaign, "credits": credits, "tzs": tzs, "left": left,
                     "open_session": open_sessions.get(campaign.pk), "locked": locked, "reason": reason,
                     "plans": sorted(plan_names[pk] for pk in plan_ids if pk in plan_names),
                     "today_done": campaign.pk in today})
    return rows


def campaign_offer_detail(member, campaign, now=None):
    """CR-01: what the campaign page shows before playback (server checks run again on start)."""
    from apps.memberships.models import MembershipPlan
    from apps.rewards import services as rewards
    from apps.rewards.terms import eligible_plan_ids

    from apps.earnings import services as earnings

    now = now or timezone.now()
    decision = advertising.campaign_eligibility(campaign, member, now)
    _policy, credits = rewards.current_offer(campaign, now)
    tzs = earnings.preview_offer(member if decision else None, campaign, now)
    budget = rewards.budget_summary(campaign)
    used = WatchSession.objects.filter(member=member, campaign=campaign, qualifying_slot__isnull=False).count()
    plan_ids = eligible_plan_ids(campaign)
    required = required_ms_for(campaign.video.duration_seconds)
    return {
        "credits": credits, "tzs": tzs, "eligible": bool(decision), "reason": decision.reason,
        "reason_message": denial_message(decision.reason) if not decision else "",
        "plans": sorted(MembershipPlan.objects.filter(pk__in=plan_ids).values_list("name", flat=True)),
        "views_left": max(0, campaign.max_views_per_member - used),
        "today_done": verified_today(member, campaign, now),
        "required_seconds": (required // 1000) if required else None,
        "budget_short": bool(credits) and budget["demo_remaining"] < credits,
    }


def member_history(member):
    return (WatchSession.objects.select_related("campaign", "campaign__organization", "video")
            .filter(member=member).order_by("-created_at"))


VERIFIED = Q(outcome=Out.VERIFIED_EVIDENCE, state=St.FINALIZED, qualifying_slot__isnull=False)


def verified_views(campaign):
    """Phase 07 boundary (read-only): sessions whose server evidence passed, one per counted slot.
    Not a reward and not payable: reward eligibility needs funded campaigns (Phase 08)."""
    return WatchSession.objects.filter(VERIFIED, campaign=campaign)


PERFORMANCE_FIGURES = {
    "sessions": Count("id"),
    "verified": Count("id", filter=VERIFIED),
    "viewers": Count("member_id", filter=VERIFIED, distinct=True),
    "in_review": Count("id", filter=Q(outcome=Out.SUSPICIOUS_REVIEW, review_status=WatchSession.Review.PENDING)),
    "not_verified": Count("id", filter=Q(outcome__in=[Out.INSUFFICIENT_EVIDENCE, Out.REJECTED, Out.EXPIRED])),
}


def campaign_performance(campaigns):
    """Viewing results for a set of campaigns, from server-decided session outcomes only.

    Returns (totals, by_campaign_id). "viewers" counts distinct members with at least one
    verified view (in the totals: across all the given campaigns). Revoked views no
    longer count (revocation clears the outcome and the counted slot). Counts only: no
    member is identified. Two aggregate queries."""
    sessions = WatchSession.objects.filter(campaign__in=campaigns)
    totals = sessions.aggregate(**PERFORMANCE_FIGURES)
    by_campaign = {row.pop("campaign_id"): row
                   for row in sessions.values("campaign_id").annotate(**PERFORMANCE_FIGURES).order_by()}
    return totals, by_campaign
