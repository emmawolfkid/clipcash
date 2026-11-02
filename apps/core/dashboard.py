"""
Member dashboard read model (Phase 11).

Read-only: every figure comes from the owning domain service (memberships,
viewing, rewards, wallets, withdrawals, payments). Nothing here calculates a
balance, creates a record, or changes state. The test suite checks that
loading the dashboard writes nothing.

Query budget: each section uses a fixed number of bounded queries (counts and
aggregates, ``[:N]`` slices, select_related). Full histories stay on their own
paginated pages.

Money semantics are kept apart on purpose:
* wallet figures = the real ledger projection (TZS 0 in this phase);
* demo reward totals = simulated records, labelled "not withdrawable";
* sandbox payment figures = test records.
"""

from dataclasses import dataclass, field

from django.db.models import Count, Q, Sum
from django.urls import reverse

from apps.core import features

ACTIVITY_LIMIT = 50


@dataclass
class Activity:
    when: object
    kind: str
    text: str
    url: str = ""
    simulated: bool = False


@dataclass
class DashboardSummary:
    membership: dict = field(default_factory=dict)
    sponsored: dict = field(default_factory=dict)
    rewards: dict = field(default_factory=dict)
    wallet: dict = field(default_factory=dict)
    withdrawals: dict = field(default_factory=dict)
    payments: dict = field(default_factory=dict)
    activity: list = field(default_factory=list)


def _membership(user):
    from apps.memberships.access import check_access
    from apps.memberships.models import Subscription

    if not features.is_enabled("memberships"):
        return {"enabled": False}
    decision = check_access(user)
    pending = (Subscription.objects.filter(member=user, status=Subscription.Status.PENDING)
               .order_by("-created_at").first())
    subscription = decision.subscription
    return {"enabled": True, "reason": decision.reason, "subscription": subscription, "pending": pending,
            "active": bool(decision), "email_verified": user.email_verified}


def _sponsored(user):
    from apps.viewing.models import WatchSession
    from apps.viewing.services import available_campaigns

    if not features.is_enabled("advertisers"):
        return {"enabled": False}
    rows = available_campaigns(user)  # Bulk pre-filter (no N+1); authoritative checks happen on start.
    counts = WatchSession.objects.filter(member=user).aggregate(
        verified=Count("id", filter=Q(outcome=WatchSession.Outcome.VERIFIED_EVIDENCE)),
        review=Count("id", filter=Q(outcome=WatchSession.Outcome.SUSPICIOUS_REVIEW)),
        total=Count("id"))
    return {"enabled": True, "available": len(rows), "top": rows[:3], **counts}


def _rewards(user):
    from apps.rewards.models import DemoReward

    if not features.is_enabled("advertisers"):
        return {"enabled": False}
    S = DemoReward.Status
    totals = DemoReward.objects.filter(member=user).aggregate(
        qualified=Count("id", filter=Q(status=S.QUALIFIED)),
        reversed=Count("id", filter=Q(status=S.REVERSED)),
        rejected=Count("id", filter=Q(status=S.REJECTED)),
        simulated_total=Sum("amount", filter=Q(status=S.QUALIFIED), default=0))
    return {"enabled": True, **totals}


def _wallet(user):
    from apps.wallets import wallet_services

    eligibility = wallet_services.wallet_eligibility(user)
    wallet = wallet_services.wallet_for(user)
    balances = wallet_services.balances(wallet) if wallet else None  # Ledger projection only.
    return {"eligible": eligibility == "ok", "eligibility": eligibility, "wallet": wallet, "balances": balances}


def _withdrawals(user):
    from apps.withdrawals import services as withdrawals
    from apps.withdrawals.models import DemoWithdrawal, PayoutBeneficiary

    decision = withdrawals.withdrawal_eligibility(user)
    destinations = PayoutBeneficiary.objects.filter(member=user, status=PayoutBeneficiary.Status.ACTIVE).count()
    return {"available": bool(decision), "reasons": decision.reasons,
            "messages": [withdrawals.ELIGIBILITY_MESSAGES.get(code, code) for code in decision.reasons[:3]],
            "destinations": destinations, "demos": DemoWithdrawal.objects.filter(member=user).count()}


def _payments(user):
    from apps.payments.models import PaymentIntent
    from apps.payments.providers import sandbox_active

    if not sandbox_active():
        return {"sandbox": False}
    counts = PaymentIntent.objects.filter(payer=user).aggregate(
        total=Count("id"), settled=Count("id", filter=Q(status=PaymentIntent.Status.SETTLED)))
    return {"sandbox": True, **counts}


def member_summary(user):
    """Everything the dashboard home shows, read-only and bounded."""
    return DashboardSummary(membership=_membership(user), sponsored=_sponsored(user), rewards=_rewards(user),
                            wallet=_wallet(user), withdrawals=_withdrawals(user), payments=_payments(user),
                            activity=member_activity(user, limit=6))


# --- Activity ---------------------------------------------------------------------------------


def member_activity(user, limit=ACTIVITY_LIMIT):
    """Safe, member-specific summaries built from the member's own domain records
    (never raw audit rows, reviewer notes, fingerprints or provider payloads).
    Each source is bounded; the merged list is sorted newest first."""
    from apps.memberships.models import Subscription
    from apps.payments.models import PaymentIntent
    from apps.rewards.models import DemoReward
    from apps.viewing.models import WatchSession
    from apps.wallets.models import Wallet, WalletRestriction
    from apps.withdrawals.models import DemoWithdrawal

    items = []
    for sub in Subscription.objects.filter(member=user).order_by("-updated_at")[:limit]:
        items.append(Activity(sub.updated_at, "membership",
                              f"Membership {sub.plan_name}: {sub.get_status_display().lower()}",
                              reverse("memberships:subscription_detail", args=[sub.pk])
                              if features.is_enabled("memberships") else ""))
    sessions = (WatchSession.objects.select_related("campaign").filter(member=user, finalized_at__isnull=False)
                .order_by("-finalized_at")[:limit])
    for session in sessions:
        items.append(Activity(session.finalized_at, "viewing",
                              f"{session.campaign.name}: {session.get_outcome_display()}",
                              reverse("viewing:session", args=[session.pk]) if features.is_enabled("advertisers")
                              else ""))
    rewards = (DemoReward.objects.select_related("campaign").filter(member=user)
               .order_by("-decided_at")[:limit])
    for reward in rewards:
        when = reward.reversed_at or reward.decided_at
        text = f"{reward.campaign.name}: {reward.get_status_display()}"
        if reward.amount:
            text += f" (TZS {reward.amount}, simulated)"
        items.append(Activity(when, "reward", text, reverse("rewards:history") if features.is_enabled("advertisers")
                              else "", simulated=True))
    for intent in PaymentIntent.objects.filter(payer=user).order_by("-updated_at")[:limit]:
        items.append(Activity(intent.updated_at, "payment",
                              f"Sandbox payment {intent.reference}: {intent.get_status_display().lower()}",
                              reverse("payments:intent", args=[intent.pk]), simulated=True))
    wallet = Wallet.objects.filter(member=user).first()
    if wallet is not None:
        items.append(Activity(wallet.created_at, "wallet", "Wallet opened", reverse("wallets:wallet")))
        for restriction in WalletRestriction.objects.filter(wallet=wallet).order_by("-updated_at")[:limit]:
            # Reasons and reviewer identities are staff-only; members see the state.
            state = "lifted" if restriction.status == WalletRestriction.Status.LIFTED else "placed under review"
            items.append(Activity(restriction.updated_at, "wallet", f"Wallet {state}", reverse("wallets:wallet")))
    for demo in DemoWithdrawal.objects.filter(member=user).order_by("-updated_at")[:limit]:
        items.append(Activity(demo.updated_at, "withdrawal", f"{demo.reference}: {demo.get_status_display()}",
                              reverse("withdrawals:demo", args=[demo.pk]), simulated=True))
    if user.email_verified:
        items.append(Activity(user.date_joined, "account", "Account created and email confirmed"))
    items = [item for item in items if item.when]
    items.sort(key=lambda item: item.when, reverse=True)
    return items[:limit]

