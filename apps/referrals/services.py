"""
REF-01: single-level referral programme.

Flow: member's referral code/link -> new member registers with it (attribution recorded once, at
registration) -> the new member completes genuine verified activity within the qualification period
-> the referral QUALIFIES -> risk signals? UNDER_REVIEW (a person decides) : the reward is allocated
-> the policy amount moves from the referral programme's verified marketing budget to the referrer's
PENDING wallet balance -> after the clearance period it becomes AVAILABLE -> FIN-02 withdrawal.

* No money for registering or for buying a membership: a reward needs the new member's own verified
  sponsored views (apps.viewing, server-verified) on several days within the qualification period.
  Membership may be required as an eligibility condition, but membership fees never fund rewards
  (database funding-separation rules; the only source is the referral programme account, which only
  verified marketing deposits can fill: apps.referrals.funding).
* Single level: only the direct referrer can ever be rewarded; nothing flows up a chain.
* Rules: each referral keeps the policy version in force at registration (qualification rules and
  the maximum amount). At allocation the LOWER of that amount and the current version's amount
  applies, and the current version must still offer rewards: a rule change can lower or stop an
  unpaid reward but never raise it.
* Concurrency: every decision locks the referral row; allocation also locks the referrer's code row
  (daily and lifetime limits) and the ledger locks the programme account (no over-commitment). One
  reward per referral (one-to-one) and one ledger entry per step (idempotency keys).
* Money moves only through apps.wallets.services and needs the referral_rewards feature (new rewards)
  or referral_settlement (clearing, reversing); both are off unless explicitly activated and never in
  a restricted demonstration. Tracking and qualification work without them (no money).
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

from apps.audit.services import DENIED, record_business_event, record_security_event
from apps.core import features
from apps.wallets import services as ledger
from apps.wallets.exceptions import InsufficientFundsError, LedgerError
from apps.wallets.models import Wallet, WalletRestriction, WalletTransaction

from .models import (
    ACTIVE_REWARD_STATES,
    CODE_ALPHABET,
    CODE_LENGTH,
    Referral,
    ReferralCode,
    ReferralPolicy,
    ReferralReward,
)

log = logging.getLogger("clipcash.referrals")
R = Referral.Status
RW = ReferralReward.Status
P = ReferralPolicy.Status
SOURCE_TYPE = "referral_reward"
PROGRAMME_ACCOUNT = "REFERRAL_PROGRAMME_FUNDING"
SESSION_KEY = "referral_code"
MIN_REASON = 5
BURST_LIMIT = 5          # More referrals than this by one referrer within 24 hours is a risk signal.
REWARD_RETRY_DAYS = 30   # Daily-limit and budget blocks are retried for this long after qualifying.
RETRYABLE = ("daily_limit", "funding_unavailable")
OPEN_STATES = (R.AWAITING, R.UNDER_REVIEW)

# Member-facing explanations (codes are stored; the text follows the reader's language).
REASON_TEXT = {
    "rewards_inactive": gettext_noop("Referral rewards were not active when this referral qualified."),
    "programme_closed": gettext_noop("The programme was not offering rewards when this referral qualified."),
    "no_reward_offered": gettext_noop("No reward was offered for referrals when this member joined."),
    "daily_limit": gettext_noop("Today's referral reward limit was reached. ClipCash tries again on the following "
                                "days."),
    "lifetime_limit": gettext_noop("You have reached the maximum number of referral rewards."),
    "funding_unavailable": gettext_noop("The referral reward budget is used up for now. ClipCash tries again later."),
    "wallet_unavailable": gettext_noop("Your account cannot receive rewards at the moment."),
    "referrer_membership": gettext_noop("An active membership is needed to earn referral rewards."),
    "not_qualified_in_time": gettext_noop("The new member did not complete the required activity within the "
                                          "qualification period."),
    "account_inactive": gettext_noop("The new member's account is not active."),
    "review_rejected": gettext_noop("After a review, this referral did not meet the programme rules."),
    "activity_revoked": gettext_noop("The new member's qualifying activity was withdrawn after a review."),
    "staff_reversed": gettext_noop("The reward was reversed after a review."),
}
STATUS_TEXT = {
    R.AWAITING: gettext_noop("Waiting for the new member to complete the required verified activity."),
    R.UNDER_REVIEW: gettext_noop("ClipCash is checking this referral before any reward."),
    R.REWARD_PENDING: gettext_noop("The reward is pending and becomes available after the clearance period."),
    R.REWARD_CLEARED: gettext_noop("The reward is in your available balance."),
}
RISK_TEXT = {
    "shared_network": gettext_noop("Same network address as the referrer"),
    "shared_device": gettext_noop("Same network address and browser as the referrer"),
    "shared_payout_destination": gettext_noop("Same payout destination as the referrer"),
    "shared_identity": gettext_noop("Same identity document details as the referrer"),
    "referral_burst": gettext_noop("Many referrals by the same referrer within 24 hours"),
    "flagged_activity": gettext_noop("Qualifying activity carried fraud flags or rejected reviews"),
    "referrer_restricted": gettext_noop("The referrer's wallet is restricted"),
}


class ReferralError(Exception):
    def __init__(self, message, *, code="invalid"):
        super().__init__(message)
        self.code = code
        self.messages = [str(message)]


class NotAuthorized(ReferralError):
    pass


def reason_message(code):
    return _(REASON_TEXT[code]) if code in REASON_TEXT else ""


def explanation(referral):
    """The member-facing sentence for one referral's current state."""
    if referral.outcome_reason in REASON_TEXT:
        return _(REASON_TEXT[referral.outcome_reason])
    if referral.status in STATUS_TEXT:
        return _(STATUS_TEXT[referral.status])
    return ""


# --- Activation and permissions -------------------------------------------------------------------


def rewards_active():
    """New referral rewards may be allocated (environment switch + approval, never in demo mode)."""
    return features.is_enabled("referral_rewards")


def settlement_active():
    """Existing referral rewards may clear or be reversed (approval only, never in demo mode)."""
    return features.is_enabled("referral_settlement")


def _staff_with(user, perm):
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.is_active
                and user.is_staff and user.has_perm(perm))


def can_view(user):
    return any(_staff_with(user, f"referrals.{perm}") for perm in (
        "view_referrals", "manage_referral_policy", "approve_referral_policy", "review_referrals",
        "record_marketing_deposits", "verify_marketing_deposits", "allocate_referral_budget"))


def can_manage_policy(user):
    return _staff_with(user, "referrals.manage_referral_policy")


def can_approve_policy(user):
    return _staff_with(user, "referrals.approve_referral_policy")


def can_review(user):
    return _staff_with(user, "referrals.review_referrals")


def _require(check, actor, action):
    if not check(actor):
        record_security_event("referrals.action.denied", outcome=DENIED,
                              actor=actor if getattr(actor, "is_authenticated", False) else None,
                              metadata={"action": action, "reason": "permission_missing"})
        raise NotAuthorized(_("You are not allowed to do this."), code="permission_missing")


def _reason(text, field="reason"):
    text = (text or "").strip()
    if len(text) < MIN_REASON:
        raise ValidationError({field: _("Please give a reason (at least 5 characters).")})
    return text[:300]


def local_day(moment):
    from apps.earnings.services import local_day as earnings_local_day

    return earnings_local_day(moment)


# --- Policy versions ---------------------------------------------------------------------------------

POLICY_FIELDS = ("tracking_open", "rewards_open", "reward_amount", "referrer_requires_membership",
                 "referred_requires_membership", "min_account_age_days", "required_verified_views",
                 "required_view_days", "qualification_days", "daily_reward_limit", "lifetime_reward_limit",
                 "clearance_hours")
BOOLEAN_FIELDS = ("tracking_open", "rewards_open", "referrer_requires_membership", "referred_requires_membership")


def current_policy():
    """The latest approved version. Version 1 (records referrals, offers no money) is built in."""
    row = ReferralPolicy.objects.filter(status=P.ACTIVE).order_by("-version").first()
    if row is None:
        try:
            with transaction.atomic():
                row = ReferralPolicy.objects.create(version=1, status=P.ACTIVE, reason="Initial defaults")
        except IntegrityError:
            row = ReferralPolicy.objects.filter(status=P.ACTIVE).order_by("-version").first()
    return row


def peek_policy():
    """The latest approved version without creating anything (read-only pages): an unsaved copy of
    the built-in defaults while no version exists yet."""
    return (ReferralPolicy.objects.filter(status=P.ACTIVE).order_by("-version").first()
            or ReferralPolicy(version=1, status=P.ACTIVE))


def pending_proposal():
    return ReferralPolicy.objects.filter(status=P.PROPOSED).order_by("-version").first()


def _int(raw, field, *, optional=False, minimum=0, maximum=1_000_000):
    text = str(raw if raw is not None else "").strip().replace(",", "")
    if text == "" and optional:
        return None
    if not text.isdigit() or not minimum <= int(text) <= maximum:
        raise ValidationError({field: _("Enter a whole number between %(min)s and %(max)s.")
                               % {"min": f"{minimum:,}", "max": f"{maximum:,}"}})
    return int(text)


def clean_policy(values):
    cleaned = {name: bool(values.get(name)) for name in BOOLEAN_FIELDS}
    cleaned.update({
        "reward_amount": _int(values.get("reward_amount") or 0, "reward_amount", maximum=1_000_000),
        "min_account_age_days": _int(values.get("min_account_age_days"), "min_account_age_days", maximum=365),
        "required_verified_views": _int(values.get("required_verified_views"), "required_verified_views",
                                        minimum=1, maximum=500),
        "required_view_days": _int(values.get("required_view_days"), "required_view_days", minimum=1, maximum=365),
        "qualification_days": _int(values.get("qualification_days"), "qualification_days", minimum=1, maximum=365),
        "daily_reward_limit": _int(values.get("daily_reward_limit"), "daily_reward_limit", optional=True, minimum=1,
                                   maximum=1000),
        "lifetime_reward_limit": _int(values.get("lifetime_reward_limit"), "lifetime_reward_limit", optional=True,
                                      minimum=1, maximum=100_000),
        "clearance_hours": _int(values.get("clearance_hours"), "clearance_hours", maximum=720),
    })
    if cleaned["rewards_open"] and cleaned["reward_amount"] <= 0:
        raise ValidationError({"reward_amount": _("Enter the reward amount, or stop offering rewards.")})
    if cleaned["required_view_days"] > min(cleaned["required_verified_views"], cleaned["qualification_days"]):
        raise ValidationError({"required_view_days": _(
            "The number of different days cannot exceed the views required or the qualification period.")})
    if cleaned["min_account_age_days"] > cleaned["qualification_days"]:
        raise ValidationError({"min_account_age_days": _(
            "The minimum account age cannot be longer than the qualification period.")})
    return cleaned


def propose_policy(actor, *, expected_version, reason, **values):
    """Propose the next version. It takes effect only when a different person approves it."""
    _require(can_manage_policy, actor, "policy.propose")
    reason = _reason(reason)
    cleaned = clean_policy(values)
    with transaction.atomic():
        current = current_policy()
        latest = ReferralPolicy.objects.select_for_update().order_by("-version").first()
        if str(current.version) != str(expected_version):
            raise ValidationError(_("The programme rules were changed by someone else. Reload and try again."))
        if ReferralPolicy.objects.filter(status=P.PROPOSED).exists():
            raise ValidationError(_("A proposed change is already awaiting approval. Decide it first."))
        changes = [f"{name}: {getattr(current, name)} -> {cleaned[name]}" for name in POLICY_FIELDS
                   if getattr(current, name) != cleaned[name]]
        if not changes:
            raise ValidationError(_("Nothing was changed."))
        proposal = ReferralPolicy.objects.create(version=latest.version + 1, base_version=current.version,
                                                 status=P.PROPOSED, created_by=actor, reason=reason, **cleaned)
        record_business_event("referrals.policy.proposed", actor=actor, target=proposal, metadata={
            "version": proposal.version, "changes": "; ".join(changes)[:200], "reason": reason[:100]})
        _notify_staff("referrals.approve_referral_policy", actor, "referrals.policy_approval_needed",
                      gettext_noop("Referral programme rules await approval"),
                      gettext_noop("Version {version} of the referral programme rules needs a second person to "
                                   "approve it."), {"version": proposal.version},
                      inbox_link=("administration:referral_programme",), key=f"policy:{proposal.pk}")
    return proposal


def decide_policy(actor, proposal, *, approve, note=""):
    """Approve or reject a proposed version (never by the person who proposed it)."""
    _require(can_approve_policy, actor, "policy.decide")
    note = (note or "").strip()[:300]
    if not approve and len(note) < MIN_REASON:
        raise ValidationError({"note": _("Please give a reason (at least 5 characters).")})
    with transaction.atomic():
        proposal = ReferralPolicy.objects.select_for_update().get(pk=proposal.pk)
        if proposal.status != P.PROPOSED:
            raise ValidationError(_("This proposal was already decided."))
        if proposal.created_by_id == actor.pk:
            raise NotAuthorized(_("A different person must decide the change you proposed."), code="four_eyes")
        if approve and proposal.base_version != current_policy().version:
            raise ValidationError(_("The rules changed after this proposal was made. Reject it and propose again."))
        proposal.status = P.ACTIVE if approve else P.REJECTED
        proposal.decided_by, proposal.decided_at, proposal.decision_note = actor, timezone.now(), note
        proposal.save(update_fields=["status", "decided_by", "decided_at", "decision_note"])
        record_business_event("referrals.policy.approved" if approve else "referrals.policy.rejected", actor=actor,
                              target=proposal, metadata={"version": proposal.version, "note": note[:100]})
    return proposal


# --- Codes and links --------------------------------------------------------------------------------


def code_eligibility(user):
    """"ok" or why the account cannot refer (members in good standing only; never staff accounts)."""
    from apps.memberships.services import member_eligibility

    if user is None or not getattr(user, "is_authenticated", False):
        return "not_authenticated"
    if user.is_staff or user.role != get_user_model().Role.MEMBER:
        return "staff_account"
    eligible, reason = member_eligibility(user)
    return "ok" if eligible else reason


def code_for(member):
    return ReferralCode.objects.filter(member=member).first()


def ensure_code(member):
    """The member's code, created once (random; a rare collision is retried)."""
    if code_eligibility(member) != "ok":
        return None
    existing = code_for(member)
    if existing is not None:
        return existing
    for _attempt in range(6):
        try:
            with transaction.atomic():
                code = ReferralCode.objects.create(member=member)
                record_business_event("referrals.code.created", actor=member, target=code, metadata={})
                return code
        except IntegrityError:
            existing = code_for(member)
            if existing is not None:
                return existing
    raise ReferralError("Could not create a referral code.", code="code_unavailable")


def normalize_code(raw):
    """Upper-case code without spaces or dashes, or None when it cannot be a referral code."""
    text = "".join(str(raw or "").split()).replace("-", "").upper()[:20]
    if len(text) != CODE_LENGTH or any(char not in CODE_ALPHABET for char in text):
        return None
    return text


def referrer_for_code(raw):
    """The ReferralCode of an eligible referrer, or None (unknown, malformed or ineligible)."""
    text = normalize_code(raw)
    if text is None:
        return None
    code = ReferralCode.objects.select_related("member").filter(code=text).first()
    if code is None or code_eligibility(code.member) != "ok":
        return None
    return code


def share_url(request, code):
    return request.build_absolute_uri(reverse("referrals:join", args=[code.code]))


def tracking_open():
    return peek_policy().tracking_open


def remember_code(request, raw):
    """Keep a referral link's code in the visitor's session until registration (or it is replaced)."""
    text = normalize_code(raw)
    if text is not None and tracking_open():
        request.session[SESSION_KEY] = text
    return text


def remembered_code(request):
    return request.session.get(SESSION_KEY, "") if hasattr(request, "session") else ""


# --- Attribution (registration only) ----------------------------------------------------------------


def attribute_new_member(user, raw_code, *, source):
    """Inside the registration transaction: record the referral for the account just created.
    A referral is never recorded later (database-enforced). Problems never block registration."""
    if not raw_code or not tracking_open():
        return None
    code = referrer_for_code(raw_code)
    if code is None or code.member_id == user.pk:
        return None
    source = source if source in Referral.Source.values else Referral.Source.CODE
    try:
        with transaction.atomic():  # Savepoint: registration succeeds even if this is refused.
            referral = Referral.objects.create(referrer=code.member, referred=user, code=code.code, source=source,
                                               policy=current_policy())
            record_business_event("referrals.referral.registered", actor=user, target=referral, metadata={
                "reference": referral.reference, "source": source, "policy_version": referral.policy.version})
            _notify(referral, "registered", gettext_noop("Someone joined with your referral code"),
                    gettext_noop("A new member joined ClipCash with your referral code. You can follow the progress "
                                 "on your Referrals page."), category="ACCOUNT")
    except (IntegrityError, DatabaseError):
        log.warning("referrals: attribution refused for a new account")
        return None
    return referral


# --- Qualification ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Progress:
    views: int
    days: int
    required_views: int
    required_days: int
    window_end: object
    account_age_ok: bool
    membership_ok: bool
    referred_active: bool

    @property
    def activity_met(self):
        return self.views >= self.required_views and self.days >= self.required_days

    @property
    def met(self):
        return self.activity_met and self.account_age_ok and self.membership_ok and self.referred_active


def _counted_sessions(referral):
    from apps.viewing.models import WatchSession

    policy = referral.policy
    start = referral.created_at
    end = start + timedelta(days=policy.qualification_days)
    return (WatchSession.objects.filter(member_id=referral.referred_id,
                                        outcome=WatchSession.Outcome.VERIFIED_EVIDENCE,
                                        state=WatchSession.State.FINALIZED, finalized_at__gte=start,
                                        finalized_at__lt=end)
            .exclude(review_status__in=[WatchSession.Review.PENDING, WatchSession.Review.REJECTED]), end)


def progress(referral, now=None):
    """The new member's genuine activity and eligibility against the referral's own policy version."""
    from apps.memberships.access import check_access
    from apps.memberships.services import member_eligibility

    now = now or timezone.now()
    policy = referral.policy
    sessions, end = _counted_sessions(referral)
    moments = list(sessions.order_by("finalized_at").values_list("finalized_at", flat=True)[:500])
    referred = referral.referred
    age_ok = now - referred.date_joined >= timedelta(days=policy.min_account_age_days)
    membership_ok = not policy.referred_requires_membership or bool(check_access(referred, now=now))
    return Progress(views=len(moments), days=len({local_day(moment) for moment in moments}),
                    required_views=policy.required_verified_views, required_days=policy.required_view_days,
                    window_end=end, account_age_ok=age_ok, membership_ok=membership_ok,
                    referred_active=member_eligibility(referred)[0])


def evaluate(referral_id, now=None):
    """Decide an AWAITING referral once its activity qualifies, or expire it after the period.
    Idempotent: anything not AWAITING is returned unchanged."""
    now = now or timezone.now()
    with transaction.atomic():
        referral = (Referral.objects.select_for_update().select_related("policy", "referred", "referrer")
                    .get(pk=referral_id))
        if referral.status != R.AWAITING:
            return referral
        state = progress(referral, now)
        if state.met:
            return _qualify(referral, now)
        if now >= state.window_end:
            referral.status, referral.decided_at = R.EXPIRED, now
            referral.outcome_reason = "account_inactive" if not state.referred_active else "not_qualified_in_time"
            referral.save(update_fields=["status", "decided_at", "outcome_reason", "updated_at"])
            record_business_event("referrals.referral.expired", actor=None, target=referral, metadata={
                "reference": referral.reference, "views": state.views, "days": state.days})
    return referral


def _qualify(referral, now):
    flags = risk_signals(referral)
    referral.qualified_at = now
    if flags:
        referral.status, referral.risk_flags = R.UNDER_REVIEW, flags
        referral.save(update_fields=["status", "qualified_at", "risk_flags", "updated_at"])
        record_business_event("referrals.referral.under_review", actor=None, target=referral, metadata={
            "reference": referral.reference, "flags": flags})
        _notify_staff("referrals.review_referrals", None, "referrals.review_needed",
                      gettext_noop("A referral awaits review"),
                      gettext_noop("Referral {reference} qualified with risk signals and needs a review before any "
                                   "reward."), {"reference": referral.reference},
                      inbox_link=("administration:referral", referral.pk), key=f"review:{referral.pk}",
                      exclude=(referral.referrer_id, referral.referred_id))
        return referral
    referral.save(update_fields=["qualified_at", "updated_at"])
    return _allocate(referral, now)


def risk_signals(referral):
    """Risk signals for a person to weigh. Shared networks, devices or payout destinations are common
    between legitimate members, so they trigger a review, never an automatic rejection."""
    from apps.audit.models import AuditEvent
    from apps.withdrawals.models import PayoutBeneficiary, WithdrawalIdentity

    referrer_id, referred_id = referral.referrer_id, referral.referred_id
    flags = []
    pairs = set(AuditEvent.objects.filter(actor_id=referred_id, ip_address__isnull=False)
                .values_list("ip_address", "user_agent")[:300])
    ips = {ip for ip, _agent in pairs}
    if ips:
        theirs = set(AuditEvent.objects.filter(actor_id=referrer_id, ip_address__in=ips)
                     .values_list("ip_address", "user_agent")[:300])
        if theirs:
            flags.append("shared_network")
            if any(agent and (ip, agent) in pairs for ip, agent in theirs):
                flags.append("shared_device")
    prints = set(PayoutBeneficiary.objects.filter(member_id=referred_id).values_list("identifier_fingerprint",
                                                                                     flat=True))
    if prints and PayoutBeneficiary.objects.filter(member_id=referrer_id, identifier_fingerprint__in=prints).exists():
        flags.append("shared_payout_destination")
    identities = {i.member_id: i for i in WithdrawalIdentity.objects.filter(member_id__in=[referrer_id, referred_id])}
    a, b = identities.get(referrer_id), identities.get(referred_id)
    if a and b and a.id_type == b.id_type and a.id_last4 == b.id_last4 \
            and a.legal_name.casefold().split() == b.legal_name.casefold().split():
        flags.append("shared_identity")
    window = timedelta(hours=24)
    if Referral.objects.filter(referrer_id=referrer_id, created_at__gte=referral.created_at - window,
                               created_at__lte=referral.created_at + window).count() > BURST_LIMIT:
        flags.append("referral_burst")
    sessions, _end = _counted_sessions(referral)
    from apps.viewing.models import WatchSession

    if (any(flag_list for flag_list in sessions.values_list("flags", flat=True)[:500])
            or WatchSession.objects.filter(member_id=referred_id, review_status=WatchSession.Review.REJECTED).exists()):
        flags.append("flagged_activity")
    if Wallet.objects.filter(member_id=referrer_id, status=Wallet.Status.RESTRICTED).exists():
        flags.append("referrer_restricted")
    return flags


def _lower(*values):
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _reward_terms(referral, now):
    """(reason, amount): why no reward can be allocated now ("" = it can), and the amount."""
    from apps.memberships.access import check_access
    from apps.wallets import wallet_services

    if not rewards_active():
        return "rewards_inactive", 0
    current = current_policy()
    attributed = referral.policy
    if not attributed.rewards_open or attributed.reward_amount <= 0:
        return "no_reward_offered", 0
    if not current.rewards_open:
        return "programme_closed", 0
    amount = min(attributed.reward_amount, current.reward_amount)
    referrer = referral.referrer
    if code_eligibility(referrer) != "ok" or wallet_services.wallet_eligibility(referrer) != "ok":
        return "wallet_unavailable", 0
    if (attributed.referrer_requires_membership or current.referrer_requires_membership) \
            and not check_access(referrer, now=now):
        return "referrer_membership", 0
    ReferralCode.objects.select_for_update().filter(member_id=referrer.pk).first()  # Serialize per referrer.
    active = ReferralReward.objects.filter(member_id=referrer.pk, status__in=ACTIVE_REWARD_STATES)
    daily = _lower(attributed.daily_reward_limit, current.daily_reward_limit)
    if daily is not None and active.filter(reward_day=local_day(now)).count() >= daily:
        return "daily_limit", 0
    lifetime = _lower(attributed.lifetime_reward_limit, current.lifetime_reward_limit)
    if lifetime is not None and active.count() >= lifetime:
        return "lifetime_limit", 0
    return "", amount


def _wallet_txn(wallet, entry, category, bucket, direction, amount):
    return WalletTransaction.objects.create(
        wallet=wallet, journal_entry=entry, reference=f"WT-{str(entry.pk)[:8].upper()}-{bucket[:1]}",
        idempotency_key=f"{entry.pk}:{bucket}", category=category, bucket=bucket, direction=direction,
        amount=amount)


def programme_account():
    return ledger.get_platform_account(PROGRAMME_ACCOUNT)


def _allocate(referral, now):
    """Inside a transaction holding the referral row: allocate the reward exactly once, or record
    why none can be allocated (QUALIFIED with a reason)."""
    from apps.wallets import wallet_services

    reason, amount = _reward_terms(referral, now)
    wallet = None
    if not reason:
        try:
            wallet = wallet_services.ensure_wallet(referral.referrer)
        except wallet_services.NotEligible:
            reason = "wallet_unavailable"
    if not reason:
        try:
            with transaction.atomic():
                entry = ledger.post_entry(
                    "referral.reward_pending",
                    lines=[ledger.debit(programme_account(), amount, "Referral reward"),
                           ledger.credit(wallet.pending_account, amount, "Referral reward pending clearance")],
                    idempotency_key=f"referral-pending:{referral.pk}", source_type=SOURCE_TYPE,
                    source_id=str(referral.pk), description=f"Referral reward {referral.reference}", actor=None)
        except InsufficientFundsError:
            reason = "funding_unavailable"
    if reason:
        first = referral.status != R.QUALIFIED
        referral.status, referral.outcome_reason, referral.decided_at = R.QUALIFIED, reason, now
        referral.save(update_fields=["status", "outcome_reason", "decided_at", "qualified_at", "reviewed_by",
                                     "review_note", "updated_at"])
        if first:
            record_business_event("referrals.referral.qualified", actor=None, target=referral, metadata={
                "reference": referral.reference, "reward": "none", "reason": reason})
            _notify(referral, "qualified", gettext_noop("A referral qualified"),
                    gettext_noop("A member you referred completed the required activity. No reward was issued: "
                                 "{reason__t}"), reason__t=REASON_TEXT[reason])
        return referral
    _wallet_txn(wallet, entry, WalletTransaction.Category.REFERRAL_CREDIT, WalletTransaction.Bucket.PENDING,
                WalletTransaction.Direction.IN, amount)
    hours = max(referral.policy.clearance_hours, current_policy().clearance_hours)
    reward = ReferralReward.objects.create(
        referral=referral, member=referral.referrer, policy_version=referral.policy.version, amount=amount,
        reward_day=local_day(now), clears_at=now + timedelta(hours=hours), pending_entry=entry)
    referral.status, referral.outcome_reason, referral.decided_at = R.REWARD_PENDING, "", now
    referral.save(update_fields=["status", "outcome_reason", "decided_at", "qualified_at", "reviewed_by",
                                 "review_note", "updated_at"])
    record_business_event("referrals.reward.pending", actor=None, target=reward, metadata={
        "reference": referral.reference, "amount": amount, "clears_at": reward.clears_at.isoformat(),
        "policy_version": referral.policy.version})
    _notify(referral, "pending", gettext_noop("Referral reward pending: TZS {amount}"),
            gettext_noop("A member you referred qualified. TZS {amount} becomes available on {clears}."),
            amount=f"{amount:,}", clears=reward.clears_at)
    return referral


def retry_allocations(now=None, limit=200):
    """Background: retry rewards blocked only by a daily limit or an empty budget (for a while)."""
    if not rewards_active():
        return 0
    now = now or timezone.now()
    ids = list(Referral.objects.filter(status=R.QUALIFIED, outcome_reason__in=RETRYABLE,
                                       qualified_at__gte=now - timedelta(days=REWARD_RETRY_DAYS))
               .order_by("qualified_at").values_list("pk", flat=True)[:limit])
    allocated = 0
    for referral_id in ids:
        with transaction.atomic():
            referral = (Referral.objects.select_for_update().select_related("policy", "referrer")
                        .get(pk=referral_id))
            if referral.status == R.QUALIFIED and referral.outcome_reason in RETRYABLE:
                if _allocate(referral, now).status == R.REWARD_PENDING:
                    allocated += 1
    return allocated


def evaluate_due(now=None, limit=500):
    """Background: evaluate open referrals (activity can also arrive without a session signal)."""
    now = now or timezone.now()
    ids = list(Referral.objects.filter(status=R.AWAITING).order_by("created_at").values_list("pk", flat=True)[:limit])
    counts = {"qualified": 0, "expired": 0, "waiting": 0}
    for referral_id in ids:
        status = evaluate(referral_id, now).status
        counts["expired" if status == R.EXPIRED else "waiting" if status == R.AWAITING else "qualified"] += 1
    return counts


def refresh_safely(referral_id):
    try:
        referral = Referral.objects.get(pk=referral_id)
        if referral.status == R.AWAITING:
            evaluate(referral_id)
        elif referral.status in (R.REWARD_PENDING, R.REWARD_CLEARED):
            check_activity(referral)
    except Exception:  # The jobs retry; a viewing request must never fail because of this.
        log.exception("referrals: refresh failed for %s", referral_id)


def on_session_changed(sender, session, **kwargs):
    """apps.viewing session_closed / session_reversed receiver: re-check the new member's referral
    after the viewing transaction commits."""
    referral_id = (Referral.objects.filter(referred_id=session.member_id)
                   .exclude(status__in=[R.REJECTED, R.REVERSED, R.EXPIRED, R.QUALIFIED])
                   .values_list("pk", flat=True).first())
    if referral_id is not None:
        transaction.on_commit(lambda: refresh_safely(referral_id))


# --- Review ----------------------------------------------------------------------------------------


def review(actor, referral, *, approve, note):
    """A reviewer's decision. Approval allocates through the normal rules (policy amount, budget,
    limits): staff can never choose an amount or grant a reward outside them."""
    _require(can_review, actor, "referral.approve" if approve else "referral.reject")
    note = _reason(note, "note")
    now = timezone.now()
    with transaction.atomic():
        referral = (Referral.objects.select_for_update().select_related("policy", "referrer", "referred")
                    .get(pk=referral.pk))
        if actor.pk in (referral.referrer_id, referral.referred_id):
            raise NotAuthorized(_("You cannot decide a referral you are part of."), code="own_referral")
        allowed = (R.UNDER_REVIEW,) if approve else (R.AWAITING, R.UNDER_REVIEW, R.QUALIFIED)
        if referral.status not in allowed:
            raise ReferralError(_("This referral was decided or changed since you opened it."), code="stale")
        referral.reviewed_by, referral.review_note = actor, note
        if approve:
            record_business_event("referrals.referral.review_approved", actor=actor, target=referral, metadata={
                "reference": referral.reference, "note": note[:100]})
            return _allocate(referral, now)
        referral.status, referral.outcome_reason, referral.decided_at = R.REJECTED, "review_rejected", now
        referral.save(update_fields=["status", "outcome_reason", "decided_at", "reviewed_by", "review_note",
                                     "updated_at"])
        record_business_event("referrals.referral.rejected", actor=actor, target=referral, metadata={
            "reference": referral.reference, "note": note[:100]})
        _notify(referral, "rejected", gettext_noop("A referral was not accepted"),
                gettext_noop("After a review, a referral did not meet the programme rules, so it earns no reward."))
    return referral


# --- Clearance, holds and reversals --------------------------------------------------------------------


def set_hold(actor, reward, *, hold, reason):
    _require(can_review, actor, "reward.hold" if hold else "reward.unhold")
    reason = _reason(reason)
    with transaction.atomic():
        reward = ReferralReward.objects.select_for_update().select_related("referral").get(pk=reward.pk)
        if actor.pk in (reward.member_id, reward.referral.referred_id):
            raise NotAuthorized(_("You cannot decide a referral you are part of."), code="own_referral")
        if reward.status != RW.PENDING:
            raise ReferralError(_("Only pending rewards can be held or released."), code="not_pending")
        if reward.on_hold == hold:
            return reward
        reward.on_hold, reward.hold_reason = hold, reason
        reward.save(update_fields=["on_hold", "hold_reason", "updated_at"])
        record_business_event("referrals.reward.held" if hold else "referrals.reward.hold_lifted", actor=actor,
                              target=reward, metadata={"reference": reward.referral.reference,
                                                       "reason": reason[:100]})
    return reward


def clear(reward_id, now=None):
    """Move one pending referral reward to the available balance. Idempotent."""
    from apps.memberships.services import member_eligibility

    now = now or timezone.now()
    with transaction.atomic():
        reward = (ReferralReward.objects.select_for_update().select_related("referral", "referral__policy",
                                                                            "referral__referred")
                  .get(pk=reward_id))
        if reward.status != RW.PENDING or reward.on_hold or reward.clears_at > now or not settlement_active():
            return reward
        referral = reward.referral
        if not member_eligibility(referral.referred)[0] or not progress(referral, now).activity_met:
            return reward  # Reconciliation decides (reversal); a disputed reward never clears.
        wallet = Wallet.objects.select_for_update().get(member_id=reward.member_id)
        if wallet.status == Wallet.Status.RESTRICTED:
            return reward
        entry = ledger.post_entry(
            "referral.reward_cleared",
            lines=[ledger.debit(wallet.pending_account, reward.amount, "Clearance period ended"),
                   ledger.credit(wallet.available_account, reward.amount, "Referral reward available")],
            idempotency_key=f"referral-cleared:{reward.pk}", source_type=SOURCE_TYPE, source_id=str(referral.pk),
            description=f"Referral reward cleared {referral.reference}", actor=None)
        _wallet_txn(wallet, entry, WalletTransaction.Category.REFERRAL_CLEARANCE, WalletTransaction.Bucket.PENDING,
                    WalletTransaction.Direction.OUT, reward.amount)
        _wallet_txn(wallet, entry, WalletTransaction.Category.REFERRAL_CLEARANCE,
                    WalletTransaction.Bucket.AVAILABLE, WalletTransaction.Direction.IN, reward.amount)
        reward.status, reward.cleared_entry, reward.cleared_at = RW.AVAILABLE, entry, now
        reward.save(update_fields=["status", "cleared_entry", "cleared_at", "updated_at"])
        Referral.objects.filter(pk=referral.pk).update(status=R.REWARD_CLEARED, updated_at=now)
        record_business_event("referrals.reward.cleared", actor=None, target=reward, metadata={
            "reference": referral.reference, "amount": reward.amount})
        _notify(referral, "available", gettext_noop("Referral reward available: TZS {amount}"),
                gettext_noop("Your referral reward has cleared and is in your available balance."),
                amount=f"{reward.amount:,}")
    return reward


def clear_due(now=None, limit=200):
    now = now or timezone.now()
    if not settlement_active():
        return {"cleared": 0, "checked": 0}
    ids = list(ReferralReward.objects.filter(status=RW.PENDING, on_hold=False, clears_at__lte=now)
               .order_by("clears_at").values_list("pk", flat=True)[:limit])
    cleared = sum(1 for reward_id in ids if clear(reward_id, now).status == RW.AVAILABLE)
    return {"cleared": cleared, "checked": len(ids)}


def _restrict_for_recovery(wallet, actor, reward):
    if wallet.restrictions.filter(status=WalletRestriction.Status.ACTIVE,
                                  kind=WalletRestriction.Kind.FRAUD_REVIEW).exists():
        return
    placed_by = actor if getattr(actor, "is_authenticated", False) else None
    if placed_by is None:
        return  # Only a person can place a restriction; reconciliation keeps retrying the recovery.
    WalletRestriction.objects.create(wallet=wallet, kind=WalletRestriction.Kind.FRAUD_REVIEW, placed_by=placed_by,
                                     reason=f"Referral reward {reward.referral.reference} reversed after clearance: "
                                            "manual recovery")
    Wallet.objects.filter(pk=wallet.pk).update(status=Wallet.Status.RESTRICTED)
    record_business_event("wallets.restriction.placed", actor=placed_by, target=wallet,
                          metadata={"kind": WalletRestriction.Kind.FRAUD_REVIEW, "reason": "referral reward recovery"})


def reverse_reward(reward, *, actor=None, reason, source):
    """Return a referral reward to the programme budget. PENDING -> REVERSED; AVAILABLE -> REVERSED
    when the available balance covers it, else RECOVERY_REQUIRED (nothing ever goes negative and no
    other money is taken). Idempotent."""
    now = timezone.now()
    outcome = "activity_revoked" if source in ("activity", "reconcile") else "staff_reversed"
    with transaction.atomic():
        reward = ReferralReward.objects.select_for_update().select_related("referral").get(pk=reward.pk)
        if reward.status not in (RW.PENDING, RW.AVAILABLE, RW.RECOVERY_REQUIRED):
            return reward
        referral = Referral.objects.select_for_update().get(pk=reward.referral_id)
        wallet = Wallet.objects.select_for_update().get(member_id=reward.member_id)
        stage = "pending" if reward.status == RW.PENDING else "available"
        account = wallet.pending_account if stage == "pending" else wallet.available_account
        posting = "referral.pending_reversed" if stage == "pending" else "referral.cleared_reversed"
        person = actor if getattr(actor, "is_authenticated", False) else None
        try:
            with transaction.atomic():
                entry = ledger.post_entry(
                    posting, lines=[ledger.debit(account, reward.amount, "Referral reward reversed"),
                                    ledger.credit(programme_account(), reward.amount, "Returned to referral budget")],
                    idempotency_key=f"referral-reversal:{reward.pk}", source_type=SOURCE_TYPE,
                    source_id=str(referral.pk), description=f"Referral reward reversed {referral.reference}",
                    actor=None)
        except InsufficientFundsError:
            if reward.status == RW.AVAILABLE:
                reward.status, reward.reversed_at, reward.reversed_by = RW.RECOVERY_REQUIRED, now, person
                reward.reversal_reason = reason[:300]
                reward.save(update_fields=["status", "reversed_at", "reversed_by", "reversal_reason", "updated_at"])
                referral.status, referral.outcome_reason, referral.decided_at = R.REVERSED, outcome, now
                referral.save(update_fields=["status", "outcome_reason", "decided_at", "updated_at"])
                _restrict_for_recovery(wallet, person, reward)
                record_business_event("referrals.reward.recovery_required", actor=person, target=reward, metadata={
                    "reference": referral.reference, "amount": reward.amount, "source": source})
            return reward
        bucket = WalletTransaction.Bucket.PENDING if stage == "pending" else WalletTransaction.Bucket.AVAILABLE
        _wallet_txn(wallet, entry, WalletTransaction.Category.REFERRAL_REVERSAL, bucket,
                    WalletTransaction.Direction.OUT, reward.amount)
        first = referral.status != R.REVERSED
        reward.status, reward.reversal_entry, reward.reversed_at = RW.REVERSED, entry, now
        reward.reversed_by = reward.reversed_by or person
        reward.reversal_reason = reward.reversal_reason or reason[:300]
        reward.save(update_fields=["status", "reversal_entry", "reversed_at", "reversed_by", "reversal_reason",
                                   "updated_at"])
        if first:
            referral.status, referral.outcome_reason, referral.decided_at = R.REVERSED, outcome, now
            referral.save(update_fields=["status", "outcome_reason", "decided_at", "updated_at"])
        record_business_event("referrals.reward.reversed", actor=person, target=reward, metadata={
            "reference": referral.reference, "amount": reward.amount, "source": source, "stage": stage,
            "reason": reason[:100]})
        _notify(referral, "reversed", gettext_noop("Referral reward reversed: TZS {amount}"),
                gettext_noop("The referral reward of TZS {amount} was reversed: {reason__t}"),
                amount=f"{reward.amount:,}", reason__t=REASON_TEXT[outcome])
    return reward


def staff_reverse(actor, reward, *, reason):
    _require(can_review, actor, "reward.reverse")
    reason = _reason(reason)
    if actor.pk in (reward.member_id, reward.referral.referred_id):
        raise NotAuthorized(_("You cannot decide a referral you are part of."), code="own_referral")
    if not settlement_active():
        raise ReferralError(_("Referral reward settlement is switched off, so no money can be posted."),
                            code="inactive")
    return reverse_reward(reward, actor=actor, reason=reason, source="staff")


def check_activity(referral):
    """Reverse a reward whose qualifying activity no longer qualifies (e.g. views revoked after review)."""
    reward = ReferralReward.objects.filter(referral=referral, status__in=[RW.PENDING, RW.AVAILABLE]).first()
    if reward is None or not settlement_active():
        return None
    if progress(referral).activity_met:
        return reward
    return reverse_reward(reward, reason="qualifying activity withdrawn", source="activity")


# --- Reconciliation --------------------------------------------------------------------------------------


def reconcile():
    """Background job body. Evaluates, retries and reverses as needed, then checks that the referral
    records, the wallet transactions and the ledger agree. Findings are audited, never auto-corrected."""
    evaluated = evaluate_due()
    allocated = retry_allocations()
    reversed_count = 0
    if settlement_active():
        rewarded = (ReferralReward.objects.filter(status__in=[RW.PENDING, RW.AVAILABLE])
                    .select_related("referral", "referral__policy", "referral__referred")[:500])
        for reward in rewarded:
            try:
                if check_activity(reward.referral).status != reward.status:
                    reversed_count += 1
            except LedgerError:
                log.warning("referrals: reversal of %s deferred", reward.pk)
        for reward in ReferralReward.objects.filter(status=RW.RECOVERY_REQUIRED)[:200]:
            try:
                reverse_reward(reward, reason="recovery retry", source="reconcile")
            except LedgerError:
                log.warning("referrals: recovery of %s still deferred", reward.pk)
    findings = check_consistency()
    return {"evaluated": evaluated, "allocated": allocated, "reversed": reversed_count, "findings": len(findings)}


def check_consistency():
    """[(code, detail)] mismatches between referral records, wallet transactions and the ledger."""
    from .funding import budget_summary

    findings = []
    summary = budget_summary()
    expected_programme = summary["committed"] - summary["released"] - summary["allocated_net"]
    if summary["programme_balance"] != expected_programme:
        findings.append(("programme_mismatch", f"ledger {summary['programme_balance']} vs commitments and rewards "
                                               f"{expected_programme}"))
    expected_unallocated = summary["verified"] - summary["committed"] + summary["released"]
    if summary["unallocated"] != expected_unallocated:
        findings.append(("marketing_mismatch", f"ledger {summary['unallocated']} vs deposits and commitments "
                                               f"{expected_unallocated}"))
    for code, field in (("pending_entry_missing_txn", "pending_entry"), ("cleared_entry_missing_txn",
                                                                       "cleared_entry")):
        missing = ReferralReward.objects.filter(**{f"{field}__isnull": False}).exclude(
            **{f"{field}__wallet_transactions__isnull": False}).count()
        if missing:
            findings.append((code, f"{missing} referral rewards"))
    status_mismatch = (ReferralReward.objects.filter(status=RW.PENDING).exclude(referral__status=R.REWARD_PENDING)
                       .count() + ReferralReward.objects.filter(status=RW.AVAILABLE)
                       .exclude(referral__status=R.REWARD_CLEARED).count())
    if status_mismatch:
        findings.append(("status_mismatch", f"{status_mismatch} referral rewards"))
    if findings:
        with transaction.atomic():
            for code, detail in findings:
                record_business_event("referrals.reconciliation.discrepancy", actor=None,
                                      metadata={"code": code, "detail": detail[:200]})
    return findings


def reward_totals():
    """(pending, cleared) referral reward totals counted in wallet balances (earnings reconciliation)."""
    rows = ReferralReward.objects.aggregate(
        pending=Coalesce(Sum("amount", filter=Q(status=RW.PENDING)), 0),
        cleared=Coalesce(Sum("amount", filter=Q(status__in=[RW.AVAILABLE, RW.RECOVERY_REQUIRED])), 0))
    return rows["pending"], rows["cleared"]


# --- Member views ------------------------------------------------------------------------------------------


def masked_name(user):
    """A recognisable but private label: first-name initial and last-name initial, never a contact."""
    first = (user.first_name or "").strip()
    last = (user.last_name or "").strip()
    if first:
        return f"{first[0].upper()}•••{(' ' + last[0].upper() + '.') if last else ''}"
    return _("New member")


def member_overview(member):
    referrals = Referral.objects.filter(referrer=member)
    counts = referrals.aggregate(
        total=Count("pk"),
        awaiting=Count("pk", filter=Q(status__in=OPEN_STATES)),
        qualified=Count("pk", filter=Q(status__in=[R.QUALIFIED, R.REWARD_PENDING, R.REWARD_CLEARED])))
    rewards = ReferralReward.objects.filter(member=member).aggregate(
        pending=Coalesce(Sum("amount", filter=Q(status=RW.PENDING)), 0),
        cleared=Coalesce(Sum("amount", filter=Q(status=RW.AVAILABLE)), 0))
    history = list(referrals.select_related("referred", "reward", "policy").order_by("-created_at")[:50])
    for referral in history:
        referral.label = masked_name(referral.referred)
        referral.explanation = explanation(referral)
    return {"counts": counts, "pending_total": rewards["pending"], "cleared_total": rewards["cleared"],
            "earned_total": rewards["pending"] + rewards["cleared"], "history": history}


def dashboard_summary(member):
    """Read-only figures for the dashboard (creates nothing)."""
    code = code_for(member)
    counts = Referral.objects.filter(referrer=member).aggregate(
        total=Count("pk"), qualified=Count("pk", filter=Q(status__in=[R.QUALIFIED, R.REWARD_PENDING,
                                                                      R.REWARD_CLEARED])))
    return {"eligible": code_eligibility(member) == "ok", "code": code, "tracking": tracking_open(), **counts}


# --- Notifications ----------------------------------------------------------------------------------------


def _notify(referral, event, title, message, *, category="REWARD", **params):
    from apps.notifications import inbox

    inbox.notify(referral.referrer, f"referrals.{event}", category=category, title=title, message=message,
                 params=params, link=inbox.link_to("referrals:overview"),
                 dedupe_key=f"referrals.{event}:{referral.pk}")


def _notify_staff(permission, actor, kind, title, message, params, *, inbox_link, key, exclude=()):
    from apps.notifications import inbox

    excluded = {getattr(actor, "pk", None), *exclude}
    recipients = [user for user in inbox.staff_with_permission(permission) if user.pk not in excluded]
    inbox.notify(recipients, kind, category=inbox.Category.OPERATIONS, title=title, message=message, params=params,
                 link=inbox.link_to(*inbox_link), dedupe_key=f"{kind}:{key}")


User = get_user_model()
