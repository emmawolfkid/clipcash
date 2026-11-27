"""
FIN-01: real TZS rewards for verified sponsored views.

Flow: eligible campaign -> TZS offer quoted and RESERVED at session start -> verified viewing ->
reward decision -> pending wallet posting -> clearance -> available wallet balance.

* The offer is a TZS reward policy (apps.rewards, versioned, four-eyes) for the campaign's approved
  revision. Its amount is fixed for the session when it starts (SessionReward.amount).
* Reservation (``quote_for_start`` + ``reserve``, inside viewing.start_session's member lock):
  the campaign's restricted funding account is row-locked and must cover every open reservation
  plus this one, and the member's daily limits (platform and plan; the lower wins) must have room.
  So an accepted offer can always be paid, and concurrent sessions cannot over-promise.
* Settlement (``settle``) happens once the session is closed: a verified view posts
  ``reward.pending`` (campaign funding -> member pending) exactly once (idempotency key per reward,
  one reward per session); anything else RELEASES the reservation, with the reason.
* Clearance (``clear_due``, a background job) moves pending rewards older than the clearance period
  to the available balance, unless the reward is on hold, the view is under review or no longer
  verified, or the wallet is restricted.
* Reversals return the money to the campaign: from pending, or from available when the member's
  available balance still covers it. Otherwise the reward is marked RECOVERY_REQUIRED and the wallet
  is restricted for manual recovery; nothing ever goes negative and no other money is taken.

Every ledger posting goes through apps.wallets.services (balanced, idempotent, overdraft-protected)
and needs the real_money_rewards feature, which is off unless explicitly activated. Simulated
credits (apps.rewards.DemoReward) are never read here.
"""

import logging
import zoneinfo
from dataclasses import dataclass
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

from apps.audit.services import DENIED, record_business_event, record_security_event
from apps.core import features
from apps.core.services import get_site_configuration
from apps.wallets import services as ledger
from apps.wallets.exceptions import InsufficientFundsError, LedgerError
from apps.wallets.models import LedgerAccount, Wallet, WalletRestriction, WalletTransaction

from .models import ACTIVE_REWARD_STATES, EarningSettings, PlanEarningLimit, SessionReward

log = logging.getLogger("clipcash.earnings")
RS = SessionReward.Status
SOURCE_TYPE = "session_reward"
MIN_REASON = 5
FUNDING_ACCOUNT = "CAMPAIGN_RESTRICTED_FUNDING"

# Member-facing explanations (reason codes are stored; text follows the reader's language).
REASON_TEXT = {
    "earning_amount_limit": gettext_noop("You have reached today's earning limit. Come back tomorrow."),
    "earning_view_limit": gettext_noop("You have reached today's limit of rewarded views. Come back tomorrow."),
    "reward_funding_unavailable": gettext_noop("This campaign's reward funding is used up for now."),
    "rewards_inactive": gettext_noop("Real TZS rewards are not active at the moment."),
    "watch_time_short": gettext_noop("Not enough of the video was watched."),
    "coverage_short": gettext_noop("Parts of the video were skipped."),
    "duration_unknown": gettext_noop("The video's length could not be confirmed."),
    "not_started": gettext_noop("Playback never started."),
    "limit_reached": gettext_noop("This campaign was already rewarded the maximum number of times."),
    "review_rejected": gettext_noop("After review, the view was not accepted."),
    "view_revoked": gettext_noop("The verified view was withdrawn."),
    "session_invalid": gettext_noop("The session ended before it could be verified."),
    "campaign_cancelled": gettext_noop("The campaign was cancelled."),
    "wallet_unavailable": gettext_noop("Your account cannot receive rewards at the moment."),
    "superseded": gettext_noop("Another video was started before this one was finished."),
}


REASON_MESSAGES = REASON_TEXT  # Codes with a member-facing explanation.


def reason_message(code):
    return _(REASON_TEXT.get(code, gettext_noop("This view did not qualify for a reward.")))


class EarningsError(Exception):
    def __init__(self, message, *, code="invalid"):
        super().__init__(message)
        self.code = code
        self.messages = [str(message)]


class NotAuthorized(EarningsError):
    pass


# --- Activation, time and settings ------------------------------------------------------------


def active():
    """Real TZS rewards may be issued (environment switch + approval, never in demo mode)."""
    return features.is_enabled("real_money_rewards")


def site_zone():
    return zoneinfo.ZoneInfo(get_site_configuration().default_timezone)


def local_day(moment):
    """The calendar day of ``moment`` in the site time zone (Africa/Dar_es_Salaam by default)."""
    return moment.astimezone(site_zone()).date()


def day_bounds(day):
    zone = site_zone()
    from datetime import datetime, time

    start = datetime.combine(day, time.min, tzinfo=zone)
    return start, start + timedelta(days=1)


def earning_settings():
    settings_row, _created = EarningSettings.objects.get_or_create(pk=1)
    return settings_row


@dataclass(frozen=True)
class Limits:
    amount: int | None
    views: int | None
    platform_amount: int | None = None
    platform_views: int | None = None
    plan_amount: int | None = None
    plan_views: int | None = None


def _lower(*values):
    present = [value for value in values if value is not None]
    return min(present) if present else None


def limits_for(plan_id):
    """Effective daily limits for members of ``plan_id``: the platform limit and the plan limit
    both apply, so the lower one wins and a plan can never exceed a platform-wide limit."""
    platform = earning_settings()
    plan = PlanEarningLimit.objects.filter(plan_id=plan_id).first() if plan_id else None
    plan_amount = plan.daily_amount_limit if plan else None
    plan_views = plan.daily_view_limit if plan else None
    return Limits(_lower(platform.daily_amount_limit, plan_amount), _lower(platform.daily_view_limit, plan_views),
                  platform.daily_amount_limit, platform.daily_view_limit, plan_amount, plan_views)


def used_today(member, day):
    """(rewards, TZS) counting toward today's limits: reserved, pending, available or under recovery."""
    row = SessionReward.objects.filter(member=member, earning_day=day, status__in=ACTIVE_REWARD_STATES).aggregate(
        n=Count("pk"), total=Coalesce(Sum("amount"), 0))
    return row["n"], row["total"]


# --- Funding ------------------------------------------------------------------------------------


def funding_account(campaign_id, *, create=False):
    scope = str(campaign_id)
    account = LedgerAccount.objects.filter(account_type=FUNDING_ACCOUNT, scope_id=scope).first()
    if account is None and create:
        account = ledger.open_account(FUNDING_ACCOUNT, scope_id=scope)
    return account


def balance_of(account):
    """Normal-side balance of a liability account (whole TZS)."""
    return -account.debit_balance if account is not None else 0


def reserved_total(campaign_id):
    return SessionReward.objects.filter(campaign_id=campaign_id, status=RS.RESERVED).aggregate(
        total=Coalesce(Sum("amount"), 0))["total"]


def campaign_funding(campaign):
    """Funding figures for one campaign (ledger-derived; see apps.earnings.funding)."""
    from .funding import campaign_summary

    return campaign_summary(campaign)


# --- Offers --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Offer:
    policy: object = None
    amount: int = 0
    reason: str = ""  # Why this member cannot earn it now ("" = can earn).

    @property
    def is_tzs(self):
        return self.policy is not None

    @property
    def message(self):
        return reason_message(self.reason) if self.reason else ""


def tzs_policy(campaign, now=None):
    """The approved TZS policy for the campaign's current approved revision, or None."""
    from apps.rewards.services import current_policy

    policy = current_policy(campaign, now)
    return policy if policy is not None and policy.kind == policy.Kind.TZS else None


def _plan_id(member, campaign, now):
    from apps.memberships.access import check_access

    access = check_access(member, entitlement=campaign.video.required_entitlement, now=now)
    return access.subscription.plan_id if access and access.subscription else None


def _blocker(member, campaign, amount, now, *, lock):
    """The reason this member cannot be offered ``amount`` now, or "" (funding and daily limits)."""
    if not active():
        return "rewards_inactive"
    account = funding_account(campaign.pk)
    if account is None:
        return "reward_funding_unavailable"
    if lock:
        account = LedgerAccount.objects.select_for_update().get(pk=account.pk)
    if balance_of(account) - reserved_total(campaign.pk) < amount:
        return "reward_funding_unavailable"
    limits = limits_for(_plan_id(member, campaign, now))
    count, total = used_today(member, local_day(now))
    if limits.views is not None and count + 1 > limits.views:
        return "earning_view_limit"
    if limits.amount is not None and total + amount > limits.amount:
        return "earning_amount_limit"
    return ""


def preview_offer(member, campaign, now=None):
    """What the member is shown before starting (no locks; start_session re-checks). While real
    rewards are switched off nothing is offered (and nothing is queried)."""
    if not active():
        return Offer()
    now = now or timezone.now()
    policy = tzs_policy(campaign, now)
    if policy is None:
        return Offer()
    reason = _blocker(member, campaign, policy.reward_amount, now, lock=False) if member else ""
    return Offer(policy, policy.reward_amount, reason)


def quote_for_start(member, campaign, now):
    """Inside start_session (member row locked): the TZS offer this session will carry, locking the
    campaign's funding account so the capacity check and the reservation are atomic. Returns an
    Offer; ``reason`` set means the session must not start (the member is told why)."""
    policy = tzs_policy(campaign, now)
    if policy is None:
        return Offer()
    if not active():
        return Offer()  # Switched off: no TZS offer (the campaign shows no reward), nothing reserved.
    reason = _blocker(member, campaign, policy.reward_amount, now, lock=True)
    return Offer(policy, policy.reward_amount, reason)


def reserve(session, offer, now):
    """Record the session's reward reservation (same transaction as the session's creation)."""
    from apps.wallets import wallet_services

    wallet_services.ensure_wallet(session.member)
    reward = SessionReward.objects.create(
        session=session, member=session.member, campaign=session.campaign, policy=offer.policy,
        policy_version=offer.policy.version, amount=offer.amount, earning_day=local_day(now), reserved_at=now)
    record_business_event("earnings.reward.reserved", actor=session.member, target=reward, metadata={
        "campaign": str(session.campaign_id), "amount": offer.amount, "policy_version": offer.policy.version})
    return reward


# --- Settlement ------------------------------------------------------------------------------------


def _session_reason(session):
    from apps.viewing.models import WatchSession

    Out = WatchSession.Outcome
    if session.review_status == WatchSession.Review.REJECTED:
        return "review_rejected" if session.reviewed_at and session.outcome == Out.REJECTED else "view_revoked"
    if session.outcome == Out.EXPIRED:
        return "not_started"
    if session.state == WatchSession.State.INVALIDATED:
        return "session_invalid"
    reasons = [code for code in (session.reasons or []) if code in REASON_MESSAGES]
    return reasons[0] if reasons else "session_invalid"


def _release(reward, now, reasons, *, notify=True):
    reward.status = RS.RELEASED
    reward.reasons = reasons
    reward.decided_at = now
    reward.save(update_fields=["status", "reasons", "decided_at", "updated_at"])
    record_business_event("earnings.reward.released", actor=None, target=reward, metadata={
        "campaign": str(reward.campaign_id), "amount": reward.amount, "reasons": reasons})
    if notify and reward.session.state != "EXPIRED":
        _notify(reward, "not_rewarded", gettext_noop("No reward for this view"),
                gettext_noop("Your view of “{name}” did not earn the TZS {amount} offered: {reason__t}"),
                reason__t=REASON_TEXT.get(reasons[0] if reasons else "", gettext_noop("This view did not qualify for a reward.")))
    return reward


def _wallet_txn(wallet, entry, category, bucket, direction, amount):
    return WalletTransaction.objects.create(
        wallet=wallet, journal_entry=entry, reference=f"WT-{str(entry.pk)[:8].upper()}-{bucket[:1]}",
        idempotency_key=f"{entry.pk}:{bucket}", category=category, bucket=bucket, direction=direction,
        amount=amount)


def settle(reward_id, now=None):
    """Decide a reserved reward once its session is closed. Idempotent and safe to retry: a reward
    that is no longer RESERVED is returned unchanged; one whose session is open or under review, or
    while real rewards are switched off, stays reserved."""
    from apps.viewing.models import WatchSession

    now = now or timezone.now()
    with transaction.atomic():
        reward = (SessionReward.objects.select_for_update()
                  .select_related("session", "campaign", "campaign__organization", "member").get(pk=reward_id))
        if reward.status != RS.RESERVED:
            return reward
        session = reward.session
        if session.is_open:
            return reward
        if session.review_status == WatchSession.Review.PENDING or session.outcome == WatchSession.Outcome.SUSPICIOUS_REVIEW:
            return reward  # Capacity stays reserved until a reviewer decides.
        verified = (session.outcome == WatchSession.Outcome.VERIFIED_EVIDENCE
                    and session.state == WatchSession.State.FINALIZED and session.qualifying_slot is not None)
        if not verified:
            return _release(reward, now, [_session_reason(session)])
        if reward.campaign.cancelled_at is not None:
            return _release(reward, now, ["campaign_cancelled"])
        if not active():
            return reward  # Switched off after the offer: keep the reservation until reactivated.
        wallet = Wallet.objects.filter(member=reward.member).first()
        if wallet is None:
            return _release(reward, now, ["wallet_unavailable"])
        account = funding_account(reward.campaign_id)
        try:
            entry = ledger.post_entry(
                "reward.pending",
                lines=[ledger.debit(account, reward.amount, "Verified view reward"),
                       ledger.credit(wallet.pending_account, reward.amount, "Reward pending clearance")],
                idempotency_key=f"reward-pending:{reward.pk}", source_type=SOURCE_TYPE, source_id=str(reward.pk),
                description=f"Reward for a verified view ({reward.campaign.name})"[:255], actor=None)
        except InsufficientFundsError:
            return _release(reward, now, ["reward_funding_unavailable"])
        _wallet_txn(wallet, entry, WalletTransaction.Category.REWARD_CREDIT, WalletTransaction.Bucket.PENDING,
                    WalletTransaction.Direction.IN, reward.amount)
        hours = earning_settings().clearance_hours
        reward.status, reward.pending_entry, reward.decided_at = RS.PENDING, entry, now
        reward.clears_at = now + timedelta(hours=hours)
        reward.reasons = ["verified"]
        reward.save(update_fields=["status", "pending_entry", "decided_at", "clears_at", "reasons", "updated_at"])
        record_business_event("earnings.reward.pending", actor=None, target=reward, metadata={
            "campaign": str(reward.campaign_id), "amount": reward.amount, "clears_at": reward.clears_at.isoformat()})
        _notify(reward, "pending", gettext_noop("Reward pending: TZS {amount}"),
                gettext_noop("Your verified view of “{name}” earned TZS {amount}. It becomes available on {clears}."),
                clears=reward.clears_at)
    return reward


def settle_safely(reward_id):
    try:
        settle(reward_id)
    except Exception:  # The job retries; a viewing request must never fail because of this.
        log.exception("earnings.settle failed for %s", reward_id)


def settle_due(limit=200):
    """Background job body: settle reservations whose sessions are closed."""
    counts = {"pending": 0, "released": 0, "waiting": 0}
    ids = list(SessionReward.objects.filter(status=RS.RESERVED).exclude(session__state__in=["CREATED", "IN_PROGRESS"])
               .order_by("reserved_at").values_list("pk", flat=True)[:limit])
    for reward_id in ids:
        reward = settle(reward_id)
        key = {RS.PENDING: "pending", RS.RELEASED: "released"}.get(reward.status, "waiting")
        counts[key] += 1
    return counts


def on_session_closed(sender, session, **kwargs):
    """apps.viewing.services.session_closed receiver: settle after the viewing transaction commits."""
    reward_id = SessionReward.objects.filter(session_id=session.pk, status=RS.RESERVED).values_list("pk", flat=True).first()
    if reward_id is not None:
        transaction.on_commit(lambda: settle_safely(reward_id))


# --- Clearance --------------------------------------------------------------------------------------


def clear(reward_id, now=None):
    """Move one pending reward to the available balance. Idempotent: anything not due is unchanged."""
    from apps.viewing.models import WatchSession

    now = now or timezone.now()
    with transaction.atomic():
        reward = SessionReward.objects.select_for_update().select_related("session", "campaign").get(pk=reward_id)
        if reward.status != RS.PENDING or reward.on_hold or reward.clears_at > now:
            return reward
        session = reward.session
        if session.outcome != WatchSession.Outcome.VERIFIED_EVIDENCE or session.review_status == WatchSession.Review.PENDING:
            return reward  # Reversal (or review) decides; never release a disputed reward.
        wallet = Wallet.objects.select_for_update().get(member_id=reward.member_id)
        if wallet.status == Wallet.Status.RESTRICTED or not active():
            return reward
        entry = ledger.post_entry(
            "reward.cleared",
            lines=[ledger.debit(wallet.pending_account, reward.amount, "Clearance period ended"),
                   ledger.credit(wallet.available_account, reward.amount, "Reward available")],
            idempotency_key=f"reward-cleared:{reward.pk}", source_type=SOURCE_TYPE, source_id=str(reward.pk),
            description=f"Reward cleared ({reward.campaign.name})"[:255], actor=None)
        _wallet_txn(wallet, entry, WalletTransaction.Category.REWARD_CLEARANCE, WalletTransaction.Bucket.PENDING,
                    WalletTransaction.Direction.OUT, reward.amount)
        _wallet_txn(wallet, entry, WalletTransaction.Category.REWARD_CLEARANCE, WalletTransaction.Bucket.AVAILABLE,
                    WalletTransaction.Direction.IN, reward.amount)
        reward.status, reward.cleared_entry, reward.cleared_at = RS.AVAILABLE, entry, now
        reward.save(update_fields=["status", "cleared_entry", "cleared_at", "updated_at"])
        record_business_event("earnings.reward.cleared", actor=None, target=reward, metadata={
            "campaign": str(reward.campaign_id), "amount": reward.amount})
        _notify(reward, "available", gettext_noop("TZS {amount} is now available"),
                gettext_noop("Your reward for “{name}” has cleared and is in your available balance."))
    return reward


def clear_due(now=None, limit=200):
    """Background job body. Safe to retry and to run concurrently: each reward is cleared once."""
    now = now or timezone.now()
    ids = list(SessionReward.objects.filter(status=RS.PENDING, on_hold=False, clears_at__lte=now)
               .order_by("clears_at").values_list("pk", flat=True)[:limit])
    cleared = 0
    for reward_id in ids:
        if clear(reward_id, now).status == RS.AVAILABLE:
            cleared += 1
    return {"cleared": cleared, "checked": len(ids)}


# --- Holds and reversals ----------------------------------------------------------------------------


def _staff_with(user, perm):
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.is_active
                and user.is_staff and user.has_perm(perm))


def can_view(user):
    return (_staff_with(user, "earnings.view_earnings") or can_manage_rewards(user)
            or _staff_with(user, "earnings.manage_earning_rules"))


def can_manage_rewards(user):
    return _staff_with(user, "earnings.manage_real_rewards")


def can_manage_rules(user):
    return _staff_with(user, "earnings.manage_earning_rules")


def _require(check, actor, action):
    if not check(actor):
        record_security_event("earnings.action.denied", outcome=DENIED,
                              actor=actor if getattr(actor, "is_authenticated", False) else None,
                              metadata={"action": action, "reason": "permission_missing"})
        raise NotAuthorized(_("You are not allowed to do this."), code="permission_missing")


def _reason(text):
    text = (text or "").strip()
    if len(text) < MIN_REASON:
        raise ValidationError({"reason": _("Please give a reason (at least 5 characters).")})
    return text[:300]


def set_hold(actor, reward, *, hold, reason):
    """Staff hold (fraud review) on a pending reward: it will not clear until the hold is lifted."""
    _require(can_manage_rewards, actor, "reward.hold" if hold else "reward.unhold")
    reason = _reason(reason)
    with transaction.atomic():
        reward = SessionReward.objects.select_for_update().get(pk=reward.pk)
        if reward.member_id == actor.pk:
            raise NotAuthorized(_("You cannot decide your own reward."), code="own_reward")
        if reward.status != RS.PENDING:
            raise EarningsError(_("Only pending rewards can be held or released."), code="not_pending")
        if reward.on_hold == hold:
            return reward
        reward.on_hold, reward.hold_reason = hold, reason
        reward.save(update_fields=["on_hold", "hold_reason", "updated_at"])
        record_business_event("earnings.reward.held" if hold else "earnings.reward.hold_lifted", actor=actor,
                              target=reward, metadata={"campaign": str(reward.campaign_id), "reason": reason[:100]})
    return reward


def _restrict_for_recovery(wallet, actor, reward):
    """Stop further clearances while staff recover an amount the wallet no longer covers."""
    if actor is None or not getattr(actor, "is_authenticated", False):
        return
    if wallet.restrictions.filter(status=WalletRestriction.Status.ACTIVE,
                                  kind=WalletRestriction.Kind.FRAUD_REVIEW).exists():
        return
    WalletRestriction.objects.create(wallet=wallet, kind=WalletRestriction.Kind.FRAUD_REVIEW, placed_by=actor,
                                     reason=f"Reward {str(reward.pk)[:8]} reversed after clearance: manual recovery")
    Wallet.objects.filter(pk=wallet.pk).update(status=Wallet.Status.RESTRICTED)
    record_business_event("wallets.restriction.placed", actor=actor, target=wallet,
                          metadata={"kind": WalletRestriction.Kind.FRAUD_REVIEW, "reason": "reward recovery"})


def reverse_reward(reward, *, actor=None, reason, source):
    """Reverse one reward (fraud, rejected review, revoked view). Returns the reward.
    RESERVED -> RELEASED; PENDING -> REVERSED (back to the campaign); AVAILABLE -> REVERSED when the
    available balance covers it, else RECOVERY_REQUIRED (wallet restricted; nothing goes negative).
    Idempotent: repeated calls change nothing."""
    now = timezone.now()
    reason = (reason or source)[:300]
    with transaction.atomic():
        reward = (SessionReward.objects.select_for_update().select_related("session", "campaign")
                  .get(pk=reward.pk))
        if reward.status == RS.RESERVED:
            return _release(reward, now, ["view_revoked"])
        if reward.status not in (RS.PENDING, RS.AVAILABLE, RS.RECOVERY_REQUIRED):
            return reward
        wallet = Wallet.objects.select_for_update().get(member_id=reward.member_id)
        account = funding_account(reward.campaign_id)
        stage = "pending" if reward.status == RS.PENDING else "available"
        bucket_account = wallet.pending_account if stage == "pending" else wallet.available_account
        posting = "reward.pending_reversed" if stage == "pending" else "reward.cleared_reversed"
        try:
            entry = ledger.post_entry(
                posting, lines=[ledger.debit(bucket_account, reward.amount, "Reward reversed"),
                                ledger.credit(account, reward.amount, "Reward returned to campaign funding")],
                idempotency_key=f"reward-reversal:{reward.pk}", source_type=SOURCE_TYPE, source_id=str(reward.pk),
                description=f"Reward reversed: {reason}"[:255], actor=None)
        except InsufficientFundsError:
            if reward.status == RS.AVAILABLE:
                reward.status, reward.reversed_at = RS.RECOVERY_REQUIRED, now
                reward.reversed_by = actor if getattr(actor, "is_authenticated", False) else None
                reward.reversal_reason = reason
                reward.save(update_fields=["status", "reversed_at", "reversed_by", "reversal_reason", "updated_at"])
                _restrict_for_recovery(wallet, actor, reward)
                record_business_event("earnings.reward.recovery_required", actor=reward.reversed_by, target=reward,
                                      metadata={"campaign": str(reward.campaign_id), "amount": reward.amount,
                                                "source": source})
            return reward
        bucket = WalletTransaction.Bucket.PENDING if stage == "pending" else WalletTransaction.Bucket.AVAILABLE
        _wallet_txn(wallet, entry, WalletTransaction.Category.REVERSAL, bucket, WalletTransaction.Direction.OUT,
                    reward.amount)
        reward.status, reward.reversal_entry, reward.reversed_at = RS.REVERSED, entry, now
        reward.reversed_by = actor if getattr(actor, "is_authenticated", False) else None
        reward.reversal_reason = reason
        reward.save(update_fields=["status", "reversal_entry", "reversed_at", "reversed_by", "reversal_reason",
                                   "updated_at"])
        record_business_event("earnings.reward.reversed", actor=reward.reversed_by, target=reward, metadata={
            "campaign": str(reward.campaign_id), "amount": reward.amount, "source": source,
            "reason": reason[:100], "stage": stage})
        _notify(reward, "reversed", gettext_noop("Reward reversed: TZS {amount}"),
                gettext_noop("The TZS {amount} reward for your view of “{name}” was reversed after a review."))
    return reward


def staff_reverse(actor, reward, *, reason):
    _require(can_manage_rewards, actor, "reward.reverse")
    if reward.member_id == actor.pk:
        raise NotAuthorized(_("You cannot decide your own reward."), code="own_reward")
    if not active() and reward.status != RS.RESERVED:
        raise EarningsError(_("Real TZS rewards are switched off, so no money can be posted."), code="inactive")
    return reverse_reward(reward, actor=actor, reason=_reason(reason), source="staff")


def on_session_reversed(sender, session, actor=None, reason="", **kwargs):
    """apps.viewing.services.session_reversed receiver (inside the viewing transaction). A failure
    (e.g. real rewards switched off) never blocks the review; ``reconcile`` retries it."""
    reward = SessionReward.objects.filter(session_id=session.pk).first()
    if reward is None:
        return
    try:
        with transaction.atomic():
            reverse_reward(reward, actor=actor, reason=reason or "view no longer verified", source="viewing")
    except LedgerError:
        log.warning("earnings: reversal of %s deferred to reconciliation", reward.pk)


# --- Reconciliation ------------------------------------------------------------------------------------


def reconcile():
    """Background job body. Retries deferred settlements and reversals, then checks that the ledger
    and the reward records agree. Findings are reported (audited), never auto-corrected."""
    from apps.viewing.models import WatchSession

    settled = settle_due()
    reversed_count = 0
    stale = SessionReward.objects.filter(status__in=[RS.PENDING, RS.AVAILABLE]).exclude(
        session__outcome=WatchSession.Outcome.VERIFIED_EVIDENCE).exclude(
        session__review_status=WatchSession.Review.PENDING)
    # FIN-02: a reversal that could not be recovered (e.g. the money was reserved for a withdrawal)
    # is retried whatever the session state, once the money is available again.
    stale = stale | SessionReward.objects.filter(status=RS.RECOVERY_REQUIRED)
    for reward in stale[:200]:
        try:
            if reverse_reward(reward, reason="view no longer verified", source="reconcile").status != reward.status:
                reversed_count += 1
        except LedgerError:
            log.warning("earnings: reversal of %s still deferred", reward.pk)
    findings = check_consistency()
    return {"settled": settled, "reversed": reversed_count, "findings": len(findings)}


def check_consistency():
    """[(code, detail)] mismatches between reward records, wallet transactions and the ledger."""
    from apps.wallets.models import JournalLine

    findings = []

    def total(qs):
        return qs.aggregate(t=Coalesce(Sum("amount"), 0))["t"]

    def ledger_total(account_ids):
        credit = JournalLine.objects.filter(account_id__in=account_ids, side="CREDIT").aggregate(
            t=Coalesce(Sum("amount"), 0))["t"]
        debit = JournalLine.objects.filter(account_id__in=account_ids, side="DEBIT").aggregate(
            t=Coalesce(Sum("amount"), 0))["t"]
        return credit - debit

    wallets = Wallet.objects.all()
    pending_ledger = ledger_total(wallets.values("pending_account_id"))
    available_ledger = ledger_total(wallets.values("available_account_id"))
    pending_records = total(SessionReward.objects.filter(status=RS.PENDING))
    available_records = total(SessionReward.objects.filter(status__in=[RS.AVAILABLE, RS.RECOVERY_REQUIRED]))
    # FIN-02: cleared rewards are now in the available balance, reserved for a withdrawal, or paid out.
    from apps.withdrawals.models import WithdrawalRequest

    reserved_ledger = ledger_total(wallets.values("reserved_account_id"))
    paid_out = WithdrawalRequest.objects.filter(status=WithdrawalRequest.Status.PAID).aggregate(
        t=Coalesce(Sum("wallet_deduction"), 0))["t"]
    if pending_ledger != pending_records:
        findings.append(("pending_mismatch", f"ledger {pending_ledger} vs rewards {pending_records}"))
    if available_ledger + reserved_ledger + paid_out != available_records:
        findings.append(("available_mismatch", f"available {available_ledger} + reserved {reserved_ledger} + paid "
                                               f"{paid_out} vs cleared rewards {available_records}"))
    for code, entry_field in (("pending_entry_missing_txn", "pending_entry"),
                              ("cleared_entry_missing_txn", "cleared_entry")):
        missing = SessionReward.objects.filter(**{f"{entry_field}__isnull": False}).exclude(
            **{f"{entry_field}__wallet_transactions__isnull": False}).count()
        if missing:
            findings.append((code, f"{missing} rewards"))
    if findings:
        with transaction.atomic():
            for code, detail in findings:
                record_business_event("earnings.reconciliation.discrepancy", actor=None,
                                      metadata={"code": code, "detail": detail[:200]})
    return findings


# --- Member views ------------------------------------------------------------------------------------


def member_summary(member):
    rewards = SessionReward.objects.filter(member=member)
    today = local_day(timezone.now())
    count, total = used_today(member, today)
    plan_id = None
    from apps.memberships.access import check_access

    access = check_access(member)
    if access and access.subscription:
        plan_id = access.subscription.plan_id
    return {
        "pending": rewards.filter(status=RS.PENDING).order_by("clears_at"),
        "recent": rewards.select_related("campaign").order_by("-reserved_at")[:20],
        "today_count": count, "today_total": total, "limits": limits_for(plan_id),
        "active": active(),
    }


def _notify(reward, event, title, message, **params):
    from apps.notifications import inbox

    inbox.notify(reward.member, f"earnings.{event}", category=inbox.Category.REWARD, title=title, message=message,
                 params={"name": reward.campaign.name, "amount": f"{reward.amount:,}", **params},
                 link=inbox.link_to("wallets:wallet"), dedupe_key=f"earnings.{event}:{reward.pk}")


def reward_for_session(session):
    return SessionReward.objects.filter(session=session).first()


User = get_user_model()
