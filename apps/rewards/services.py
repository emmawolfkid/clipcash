"""
Demo reward services (Phase 07): policies, eligibility, evaluation, reversals.

DEMONSTRATION ONLY. Nothing here creates money, balances, obligations, wallet
credits, ledger postings or payments, and nothing can: the rewards app never
imports apps.wallets, and demo records have no path to the ledger. Real-money
rewards need reconciled advertiser funding (Phase 08), the locked
``real_money_rewards`` feature and a separate reviewed implementation.

Eligibility is judged at the verified-view event: the policy in force when the
session was finalized, for the campaign revision the member watched. A
campaign that later completes normally does not disqualify past views; fraud
reversals, staff revocations and campaign cancellation do.
"""

from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.advertising.models import Campaign, Organization
from apps.audit.services import DENIED, record_business_event, record_security_event
from apps.core import features
from apps.core.money import to_amount
from apps.viewing.models import WatchSession

from .models import CampaignRewardBudget, DemoReward, RewardPolicy

P = RewardPolicy.Status
R = DemoReward.Status
Out = WatchSession.Outcome
EVALUATION_BATCH = 200
MIN_REASON = 5
POLICY_CAMPAIGN_STATES = (Campaign.Status.SCHEDULED, Campaign.Status.ACTIVE, Campaign.Status.PAUSED,
                          Campaign.Status.COMPLETED)


class RewardsError(Exception):
    code = "rewards_error"

    def __init__(self, message, *, code=None):
        super().__init__(message)
        if code:
            self.code = code


class NotAuthorized(RewardsError):
    code = "not_authorized"


class InvalidState(RewardsError):
    code = "invalid_state"


class StaleDecision(RewardsError):
    code = "stale"


# --- Permissions ------------------------------------------------------------------------


def _staff_with(user, perm):
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.is_active
                and user.is_staff and user.has_perm(perm))


def can_manage_policies(user):
    return _staff_with(user, "rewards.manage_reward_policies")


def can_approve_policies(user):
    return _staff_with(user, "rewards.approve_reward_policies")


def can_manage_rewards(user):
    return _staff_with(user, "rewards.manage_demo_rewards")


def can_use_rewards_console(user):
    return can_manage_policies(user) or can_approve_policies(user) or can_manage_rewards(user)


def _require(check, actor, action, target=None, reason="permission_missing"):
    if not check(actor):
        record_security_event("rewards.action.denied", outcome=DENIED,
                              actor=actor if getattr(actor, "is_authenticated", False) else None,
                              target=target, metadata={"action": action, "reason": reason})
        raise NotAuthorized("You are not allowed to do this.", code=reason)


def _reason(text):
    text = (text or "").strip()
    if len(text) < MIN_REASON:
        raise ValidationError({"reason": "Please give a reason (at least 5 characters)."})
    return text[:300]


# --- Policies ---------------------------------------------------------------------------


def create_policy(actor, campaign, *, reward_amount, notes=""):
    """Draft a new policy version for the campaign's current approved revision."""
    _require(can_manage_policies, actor, "policy.create", campaign)
    try:
        amount = to_amount(reward_amount)
    except (ValueError, TypeError):
        raise ValidationError({"reward_amount": "Enter a whole number of shillings."}) from None
    with transaction.atomic():
        campaign = Campaign.objects.select_for_update(of=("self",)).get(pk=campaign.pk)
        _check_policy_campaign(campaign, amount)
        version = (RewardPolicy.objects.filter(campaign=campaign).order_by("-version")
                   .values_list("version", flat=True).first() or 0) + 1
        policy = RewardPolicy.objects.create(campaign=campaign, campaign_revision=campaign.approved_revision,
                                             version=version, reward_amount=amount, notes=(notes or "")[:300],
                                             created_by=actor)
        record_business_event("rewards.policy.created", actor=actor, target=policy,
                              metadata={"campaign": str(campaign.pk), "version": version, "amount": amount})
        _notify_approvers(policy, actor)
    return policy


# --- Notifications (Phase 15-A) ----------------------------------------------------------------
# Every reward text says "simulated" and states no amount: demo figures must never read as money.


def _notify_member(reward, event, title, message):
    from apps.notifications import inbox

    inbox.notify(reward.member, f"rewards.{event}", category=inbox.Category.REWARD, title=title,
                 message=message.format(name=reward.campaign.name), link=inbox.link_to("rewards:history"),
                 dedupe_key=f"rewards.{event}:{reward.pk}")


def _notify_approvers(policy, drafter):
    """Four-eyes: ask the other approvers (never the drafter) to decide."""
    from apps.notifications import inbox

    approvers = [user for user in inbox.staff_with_permission("rewards.approve_reward_policies")
                 if user.pk != drafter.pk]
    inbox.notify(approvers, "rewards.policy_review_needed", category=inbox.Category.REWARD,
                 title="Demo reward policy awaiting a second approver",
                 message=f"Version {policy.version} for “{policy.campaign.name}” needs approval by someone "
                         "other than its author.",
                 link=inbox.link_to("administration:reward_policy", policy.pk),
                 dedupe_key=f"rewards.policy_review_needed:{policy.pk}")


def _check_policy_campaign(campaign, amount):
    if campaign.status not in POLICY_CAMPAIGN_STATES or campaign.approved_revision != campaign.revision:
        raise ValidationError({"campaign": "Demo reward policies need an approved campaign."})
    if amount <= 0:
        raise ValidationError({"reward_amount": "Enter an amount greater than zero."})
    if amount > campaign.demo_reward_per_view:
        raise ValidationError({"reward_amount": f"Cannot exceed the campaign's approved demo reward per view "
                                                f"(TZS {campaign.demo_reward_per_view})."})


def approve_policy(actor, policy, *, expected_status=P.DRAFT):
    """Four-eyes approval. Retires the campaign's previously approved policy."""
    _require(can_approve_policies, actor, "policy.approve", policy)
    if policy.created_by_id == actor.pk:
        _require(lambda user: False, actor, "policy.approve", policy, reason="four_eyes")
    with transaction.atomic():
        campaign = Campaign.objects.select_for_update(of=("self",)).get(pk=policy.campaign_id)
        policy = RewardPolicy.objects.select_for_update().get(pk=policy.pk)
        if policy.status == P.APPROVED and expected_status == P.APPROVED:
            return policy
        if policy.status != expected_status:
            raise StaleDecision("This policy changed since you opened it.")
        if policy.status != P.DRAFT:
            raise InvalidState("Only draft policies can be approved.")
        if policy.campaign_revision != campaign.approved_revision or campaign.revision != campaign.approved_revision:
            raise InvalidState("The campaign was changed after this draft. Create a new policy version.",
                               code="campaign_revision_changed")
        _check_policy_campaign(campaign, policy.reward_amount)
        now = timezone.now()
        previous = RewardPolicy.objects.select_for_update().filter(campaign=campaign, status=P.APPROVED).first()
        if previous is not None:
            previous.status = P.RETIRED
            previous.effective_until = now
            previous.save(update_fields=["status", "effective_until", "updated_at"])
            record_business_event("rewards.policy.retired", actor=actor, target=previous, metadata={
                "campaign": str(campaign.pk), "version": previous.version, "reason": "replaced"})
        policy.status = P.APPROVED
        policy.effective_from = now
        policy.decided_by = actor
        policy.decided_at = now
        policy.save()
        record_business_event("rewards.policy.approved", actor=actor, target=policy, metadata={
            "campaign": str(campaign.pk), "version": policy.version, "amount": policy.reward_amount,
            "replaces": previous.version if previous else 0})
    return policy


def reject_policy(actor, policy, *, reason, expected_status=P.DRAFT):
    _require(can_approve_policies, actor, "policy.reject", policy)
    reason = _reason(reason)
    with transaction.atomic():
        policy = RewardPolicy.objects.select_for_update().get(pk=policy.pk)
        if policy.status == P.REJECTED and expected_status == P.REJECTED:
            return policy
        if policy.status != expected_status:
            raise StaleDecision("This policy changed since you opened it.")
        if policy.status != P.DRAFT:
            raise InvalidState("Only draft policies can be rejected.")
        policy.status = P.REJECTED
        policy.decided_by = actor
        policy.decided_at = timezone.now()
        policy.decision_reason = reason
        policy.save()
        record_business_event("rewards.policy.rejected", actor=actor, target=policy, metadata={
            "campaign": str(policy.campaign_id), "version": policy.version, "reason": reason[:100]})
    return policy


def retire_policy(actor, policy, *, reason):
    """Stop new demo rewards under this policy. Past decisions are untouched."""
    _require(can_approve_policies, actor, "policy.retire", policy)
    reason = _reason(reason)
    with transaction.atomic():
        policy = RewardPolicy.objects.select_for_update().get(pk=policy.pk)
        if policy.status == P.RETIRED:
            return policy
        if policy.status != P.APPROVED:
            raise InvalidState("Only approved policies can be retired.")
        policy.status = P.RETIRED
        policy.effective_until = timezone.now()
        policy.decision_reason = reason
        policy.save()
        record_business_event("rewards.policy.retired", actor=actor, target=policy, metadata={
            "campaign": str(policy.campaign_id), "version": policy.version, "reason": reason[:100]})
    return policy


def policy_at(campaign_id, campaign_revision, when):
    """The approved (possibly since retired) policy that was in force at ``when``."""
    return (RewardPolicy.objects.filter(campaign_id=campaign_id, campaign_revision=campaign_revision,
                                        status__in=[P.APPROVED, P.RETIRED], effective_from__lte=when)
            .filter(Q(effective_until__isnull=True) | Q(effective_until__gt=when))
            .order_by("-version").first())


# --- Eligibility ------------------------------------------------------------------------------


@dataclass
class Eligibility:
    eligible: bool
    reasons: list = field(default_factory=list)
    retry_later: bool = False  # A transient condition: no decision is recorded yet.
    policy: RewardPolicy | None = None
    # Always demonstration-only: there is no real-money outcome in this phase.
    demo_only: bool = True

    def __bool__(self):
        return self.eligible


def reward_eligibility(session, now=None):
    """
    Centralized demo reward eligibility for one watch session (server data only).
    Permanent reasons: session_not_verified, session_not_finalized, slot_missing,
    campaign_cancelled, revision_mismatch, no_policy, policy_amount_exceeds_campaign.
    Retry-later reasons: feature_disabled, review_pending, organization_not_in_good_standing,
    campaign_paused_by_staff.
    """
    if not features.is_enabled("advertisers"):
        return Eligibility(False, ["feature_disabled"], retry_later=True)
    if session.review_status == WatchSession.Review.PENDING or session.outcome == Out.SUSPICIOUS_REVIEW:
        return Eligibility(False, ["review_pending"], retry_later=True)
    if session.outcome != Out.VERIFIED_EVIDENCE:
        return Eligibility(False, ["session_not_verified"])
    if session.state != WatchSession.State.FINALIZED or session.finalized_at is None:
        return Eligibility(False, ["session_not_finalized"])
    if session.qualifying_slot is None:
        return Eligibility(False, ["slot_missing"])
    campaign = session.campaign
    if campaign.cancelled_at is not None:
        return Eligibility(False, ["campaign_cancelled"])
    if campaign.organization.verification_status != Organization.Verification.APPROVED:
        return Eligibility(False, ["organization_not_in_good_standing"], retry_later=True)
    if campaign.status == Campaign.Status.PAUSED and campaign.paused_by_staff:
        return Eligibility(False, ["campaign_paused_by_staff"], retry_later=True)
    policy = policy_at(campaign.pk, session.campaign_revision, session.finalized_at)
    if policy is None:
        return Eligibility(False, ["no_policy"])
    if policy.campaign_revision != session.campaign_revision:
        return Eligibility(False, ["revision_mismatch"])
    if campaign.revision == session.campaign_revision and policy.reward_amount > campaign.demo_reward_per_view:
        return Eligibility(False, ["policy_amount_exceeds_campaign"])
    return Eligibility(True, ["eligible"], policy=policy)


# --- Evaluation ------------------------------------------------------------------------------


def _locked_budget(campaign_id):
    CampaignRewardBudget.objects.get_or_create(campaign_id=campaign_id)
    return CampaignRewardBudget.objects.select_for_update().get(campaign_id=campaign_id)


def demo_allocated(campaign):
    return campaign.demo_budget_amount


def evaluate_session(session_id, *, actor=None):
    """Record the one demo reward decision for a session. Idempotent: returns the existing
    decision if there is one, or None while a retry-later condition holds."""
    with transaction.atomic():
        session = (WatchSession.objects.select_for_update(of=("self",))
                   .select_related("campaign", "campaign__organization").get(pk=session_id))
        existing = DemoReward.objects.filter(session=session).first()
        if existing is not None:
            if actor is not None:
                record_security_event("rewards.duplicate.refused", outcome=DENIED, actor=actor, target=existing,
                                      metadata={"campaign": str(session.campaign_id)})
            return existing
        decision = reward_eligibility(session)
        if decision.retry_later:
            return None
        now = timezone.now()
        base = {"session": session, "member_id": session.member_id, "campaign_id": session.campaign_id,
                "campaign_revision": session.campaign_revision, "decided_at": now}
        if not decision:
            reward = DemoReward.objects.create(status=R.REJECTED, amount=0, reasons=decision.reasons, **base)
            record_business_event("rewards.decision.rejected", actor=None, target=reward, metadata={
                "campaign": str(session.campaign_id), "reasons": decision.reasons})
            return reward
        policy = decision.policy
        amount = policy.reward_amount
        budget = _locked_budget(session.campaign_id)  # Lock order: session -> budget.
        allocated = demo_allocated(session.campaign)
        if budget.demo_consumed + amount > allocated:
            reward = DemoReward.objects.create(status=R.REJECTED, amount=0, reasons=["budget_exhausted"],
                                               policy=policy, policy_version=policy.version, **base)
            record_business_event("rewards.budget.exhausted", actor=None, target=reward, metadata={
                "campaign": str(session.campaign_id), "demo_allocated": allocated,
                "demo_consumed": budget.demo_consumed, "amount": amount})
            record_business_event("rewards.decision.rejected", actor=None, target=reward, metadata={
                "campaign": str(session.campaign_id), "reasons": ["budget_exhausted"]})
            return reward
        budget.demo_consumed += amount
        budget.save()
        reward = DemoReward.objects.create(status=R.QUALIFIED, amount=amount, reasons=["eligible"], policy=policy,
                                           policy_version=policy.version, **base)
        record_business_event("rewards.decision.qualified", actor=None, target=reward, metadata={
            "campaign": str(session.campaign_id), "policy_version": policy.version, "amount": amount,
            "demo_consumed": budget.demo_consumed})
        _notify_member(reward, "recorded", "Demo reward recorded (simulated, not money)",
                       "Your verified view of “{name}” earned a simulated demo reward. It is not money and "
                       "cannot be withdrawn.")
    return reward


def pending_sessions(limit=EVALUATION_BATCH):
    """Verified sessions with no demo reward decision yet (bounded, oldest first)."""
    return (WatchSession.objects.filter(outcome=Out.VERIFIED_EVIDENCE, state=WatchSession.State.FINALIZED,
                                        demo_reward__isnull=True)
            .order_by("finalized_at").values_list("pk", flat=True)[:limit])


def evaluate_pending(limit=EVALUATION_BATCH):
    """Background job body. Safe to replay and to run concurrently (unique per session)."""
    counts = {"qualified": 0, "rejected": 0, "waiting": 0}
    for session_id in list(pending_sessions(limit)):
        try:
            reward = evaluate_session(session_id)
        except IntegrityError:
            continue  # A concurrent worker recorded this session's decision first.
        if reward is None:
            counts["waiting"] += 1
        else:
            counts["qualified" if reward.status == R.QUALIFIED else "rejected"] += 1
    return counts


# --- Reversals ----------------------------------------------------------------------------------


def _reverse_locked(reward, *, actor, reason, source):
    """Inside a transaction. Releases simulated capacity exactly once."""
    reward = DemoReward.objects.select_for_update().get(pk=reward.pk)
    if reward.status != R.QUALIFIED:
        return reward, False
    budget = _locked_budget(reward.campaign_id)
    budget.demo_consumed -= reward.amount
    budget.save()
    reward.status = R.REVERSED
    reward.reversed_at = timezone.now()
    reward.reversed_by = actor if getattr(actor, "is_authenticated", False) else None
    reward.reversal_reason = reason[:300]
    reward.save()
    _notify_member(reward, "reversed", "Demo reward reversed (simulated)",
                   "A simulated demo reward for “{name}” was reversed. No money was involved.")
    record_business_event("rewards.reward.reversed", actor=actor if reward.reversed_by else None, target=reward,
                          metadata={"campaign": str(reward.campaign_id), "amount": reward.amount, "source": source,
                                    "reason": reason[:100]})
    return reward, True


def on_session_reversed(sender, session, actor=None, reason="", **kwargs):
    """apps.viewing.services.session_reversed receiver: runs in the viewing transaction."""
    reward = DemoReward.objects.filter(session_id=session.pk).first()
    if reward is not None:
        _reverse_locked(reward, actor=actor, reason=reason or "Viewing session revoked", source="viewing_review")


def reverse_reward(actor, reward, *, reason, expected_status=R.QUALIFIED):
    """Administrative reversal of one demo reward (the session is not changed)."""
    _require(can_manage_rewards, actor, "reward.reverse", reward)
    reason = _reason(reason)
    with transaction.atomic():
        WatchSession.objects.select_for_update().get(pk=reward.session_id)  # Lock order: session -> reward.
        current = DemoReward.objects.select_for_update().get(pk=reward.pk)
        if current.status == R.REVERSED and expected_status == R.REVERSED:
            return current
        if current.status != expected_status:
            raise StaleDecision("This reward changed since you opened it.")
        if current.status != R.QUALIFIED:
            raise InvalidState("Only recorded demo rewards can be reversed.")
        reward, _ = _reverse_locked(current, actor=actor, reason=reason, source="administrative")
    return reward


def reconcile_reversals(limit=EVALUATION_BATCH):
    """Backstop job: reverse qualified demo rewards whose session is no longer verified."""
    stale = list(DemoReward.objects.filter(status=R.QUALIFIED).exclude(session__outcome=Out.VERIFIED_EVIDENCE)
                 .values_list("pk", "session_id")[:limit])
    reversed_count = 0
    for reward_id, session_id in stale:
        with transaction.atomic():
            session = WatchSession.objects.select_for_update().get(pk=session_id)
            if session.outcome == Out.VERIFIED_EVIDENCE:
                continue
            _, changed = _reverse_locked(DemoReward(pk=reward_id), actor=None,
                                         reason="Viewing session no longer verified", source="reconciliation")
            reversed_count += changed
    return reversed_count


# --- Reporting -----------------------------------------------------------------------------------


def campaign_totals(campaigns=None):
    """Per-campaign simulated totals in one query: [{campaign, qualified, reversed, rejected, demo_consumed}]."""
    qs = DemoReward.objects.all() if campaigns is None else DemoReward.objects.filter(campaign__in=campaigns)
    return (qs.values("campaign_id", "campaign__name", "campaign__demo_budget_amount")
            .annotate(qualified=Count("id", filter=Q(status=R.QUALIFIED)),
                      reversed=Count("id", filter=Q(status=R.REVERSED)),
                      rejected=Count("id", filter=Q(status=R.REJECTED)),
                      demo_consumed=Sum("amount", filter=Q(status=R.QUALIFIED), default=0))
            .order_by("campaign__name"))


def budget_summary(campaign):
    budget = CampaignRewardBudget.objects.filter(campaign=campaign).first()
    consumed = budget.demo_consumed if budget else 0
    allocated = demo_allocated(campaign)
    return {"demo_allocated": allocated, "demo_consumed": consumed, "demo_remaining": max(0, allocated - consumed)}


def member_rewards(member):
    return (DemoReward.objects.select_related("campaign", "campaign__organization")
            .filter(member=member).order_by("-decided_at"))


def member_totals(member):
    """Per-campaign simulated totals for one member (qualified only count)."""
    return (DemoReward.objects.filter(member=member)
            .values("campaign_id", "campaign__name")
            .annotate(qualified=Count("id", filter=Q(status=R.QUALIFIED)),
                      reversed=Count("id", filter=Q(status=R.REVERSED)),
                      demo_total=Sum("amount", filter=Q(status=R.QUALIFIED), default=0))
            .order_by("campaign__name"))


REASON_MESSAGES = {
    "eligible": "Recorded under an approved demo policy.",
    "session_not_verified": "The view was not verified.",
    "session_not_finalized": "The viewing session was not finished.",
    "slot_missing": "The view was not counted.",
    "campaign_cancelled": "The campaign was cancelled.",
    "revision_mismatch": "The campaign changed after this view.",
    "no_policy": "No approved demo reward policy applied at the time of the view.",
    "policy_amount_exceeds_campaign": "The policy exceeded the campaign's approved amount.",
    "budget_exhausted": "The campaign's demo budget was used up.",
}


def reason_text(codes):
    return " ".join(REASON_MESSAGES.get(code, code.replace("_", " ")) for code in codes or [])
