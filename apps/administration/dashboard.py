"""
OPS-01: the Operations Console business dashboard (read-only).

Only real figures the staff member may see: each card, task and amount is computed only when the
staff member holds the permission of the page it links to (console_links, the same checks those pages
use). Money is never mixed: confirmed membership income, member reward balances (ledger) and money
actually paid out are separate lines, and sandbox or demo records are never included. Nothing here
writes to the database or calls an external service.
"""

from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.db.models import Count, Q, Sum
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.templatetags.money import tzs

from . import summary


@dataclass(frozen=True)
class Task:
    label: object
    count: int
    url: str


def _url(name, query=""):
    return reverse(f"administration:{name}") + query


def _stats(user, can):
    from apps.payments import manual as manual_payments

    User = get_user_model()
    stats = []
    if can["members"]:
        members = User.objects.filter(role=User.Role.MEMBER).aggregate(
            total=Count("id"), active=Count("id", filter=Q(status=User.Status.ACTIVE)))
        members_data = summary._members()
        stats.append({"label": _("Members"), "value": f"{members['total']:,}",
                      "note": _("%(n)s active accounts") % {"n": f"{members['active']:,}"},
                      "url": _url("members")})
        stats.append({"label": _("Active memberships"), "value": f"{members_data['current_memberships']:,}",
                      "note": _("Current paid or granted plans"), "url": _url("plans") if can["plans"] else ""})
    if manual_payments.can_view(user):
        from apps.payments.models import ManualMembershipPayment

        approved = ManualMembershipPayment.objects.filter(status=ManualMembershipPayment.Status.APPROVED).aggregate(
            n=Count("id"), total=Sum("amount", default=0))
        stats.append({"label": _("Confirmed payments"), "value": f"{approved['n']:,}",
                      "note": tzs(approved["total"]),
                      "url": _url("membership_payments", "?status=APPROVED") if can["membership_payments"] else ""})
    if can["withdrawal_requests"]:
        from apps.withdrawals.models import WithdrawalRequest

        W = WithdrawalRequest.Status
        open_states = [W.REQUESTED, W.UNDER_REVIEW, W.APPROVED, W.PAYOUT_PENDING, W.VERIFYING, W.RECONCILIATION]
        pending = WithdrawalRequest.objects.filter(status__in=open_states).aggregate(
            n=Count("id"), total=Sum("amount", default=0))
        stats.append({"label": _("Pending withdrawals"), "value": f"{pending['n']:,}",
                      "note": tzs(pending["total"]), "url": _url("withdrawal_requests")})
    return stats


def _tasks(user, can):
    tasks = []

    def add(label, count, url):
        if count:
            tasks.append(Task(label, count, url))

    if can["membership_payments"]:
        from apps.payments.models import ManualMembershipPayment

        add(_("Membership payments to verify"),
            ManualMembershipPayment.objects.filter(status=ManualMembershipPayment.Status.PENDING_VERIFICATION).count(),
            _url("membership_payments"))
    if can["withdrawal_requests"]:
        from apps.withdrawals.models import WithdrawalRequest

        W = WithdrawalRequest.Status
        counts = dict(WithdrawalRequest.objects.values_list("status").annotate(n=Count("id")))
        add(_("Withdrawal requests to review"), counts.get(W.REQUESTED, 0) + counts.get(W.UNDER_REVIEW, 0),
            _url("withdrawal_requests", "?status=REQUESTED"))
        add(_("Approved withdrawals to send"), counts.get(W.APPROVED, 0), _url("withdrawal_requests", "?status=APPROVED"))
        add(_("Sent payouts to verify"), counts.get(W.VERIFYING, 0), _url("withdrawal_requests", "?status=VERIFYING"))
        add(_("Payouts needing reconciliation"), counts.get(W.RECONCILIATION, 0),
            _url("withdrawal_requests", "?status=RECONCILIATION"))
    if can["withdrawal_identities"]:
        from apps.withdrawals.models import WithdrawalIdentity

        add(_("Identity checks to review"), WithdrawalIdentity.objects.filter(status="SUBMITTED").count(),
            _url("withdrawal_identities"))
    if can["campaigns"]:
        from apps.advertising.models import Campaign

        add(_("Campaigns awaiting approval"), Campaign.objects.filter(status=Campaign.Status.SUBMITTED).count(),
            _url("campaigns", "?status=SUBMITTED"))
    if can["advertisers"]:
        from apps.advertising.models import Organization

        add(_("Advertisers awaiting review"),
            Organization.objects.filter(verification_status=Organization.Verification.PENDING).count(),
            _url("advertisers"))
    if can["referrals"]:
        from apps.referrals.models import MarketingDeposit, Referral, ReferralPolicy

        add(_("Referrals awaiting review"), Referral.objects.filter(status=Referral.Status.UNDER_REVIEW).count(),
            _url("referrals", "?status=UNDER_REVIEW"))
        add(_("Referral rule changes to approve"), ReferralPolicy.objects.filter(status="PROPOSED").count(),
            _url("referral_programme"))
        add(_("Marketing deposits to verify"), MarketingDeposit.objects.filter(status="RECORDED").count(),
            _url("referral_funding", "?status=RECORDED"))
    if can["campaign_funding"]:
        from apps.earnings.models import CampaignDeposit

        add(_("Campaign deposits to verify"), CampaignDeposit.objects.filter(status="RECORDED").count(),
            _url("campaign_funding", "?status=RECORDED"))
    if can["rewards"]:
        from apps.rewards.models import RewardPolicy

        add(_("Reward rates awaiting approval"), RewardPolicy.objects.filter(status=RewardPolicy.Status.DRAFT).count(),
            _url("reward_policies"))
    if can["viewing"]:
        from apps.viewing.models import WatchSession

        add(_("Views awaiting review"), WatchSession.objects.filter(review_status=WatchSession.Review.PENDING).count(),
            _url("viewing", "?review=1"))
    if can["support"]:
        from apps.support.models import Ticket

        add(_("Open support requests"), Ticket.objects.filter(status__in=["OPEN", "IN_PROGRESS"]).count(),
            _url("support"))
    if can["wallets"]:
        from apps.wallets.models import WalletDiscrepancy

        add(_("Wallet check findings"), WalletDiscrepancy.objects.filter(status="OPEN").count(),
            _url("wallet_reconciliation"))
    if can["videos"]:
        add(_("Video uploads that did not finish"), summary._videos()["stuck"], _url("videos"))
    return tasks


def _money(user, can):
    """Three separate real-money lines (whole TZS); None when the staff member sees no finance."""
    from apps.payments import manual as manual_payments

    lines = []
    if manual_payments.can_view(user):
        from apps.payments.models import ManualMembershipPayment

        income = ManualMembershipPayment.objects.filter(status=ManualMembershipPayment.Status.APPROVED).aggregate(
            t=Sum("amount", default=0))["t"]
        lines.append((_("Confirmed membership income"), income))
    if can["wallets"] or can["video_rewards"]:
        from apps.wallets.models import JournalLine

        owed = JournalLine.objects.filter(account__account_type__in=[
            "MEMBER_REWARD_PENDING", "REWARD_LIABILITY", "MEMBER_WITHDRAWAL_RESERVED"]).aggregate(
            credit=Sum("amount", filter=Q(side="CREDIT"), default=0), debit=Sum("amount", filter=Q(side="DEBIT"), default=0))
        lines.append((_("Rewards owed to members (funded)"), owed["credit"] - owed["debit"]))
    if can["withdrawal_requests"]:
        from apps.withdrawals.models import WithdrawalRequest

        paid = WithdrawalRequest.objects.filter(status=WithdrawalRequest.Status.PAID).aggregate(
            t=Sum("net_payout", default=0))["t"]
        lines.append((_("Paid out to members"), paid))
    return lines or None


def _activity(user, can, limit=6):
    """Latest real events from records this staff member may see (newest first)."""
    from apps.payments import manual as manual_payments

    User = get_user_model()
    rows = []
    if can["members"]:
        for member in User.objects.filter(role=User.Role.MEMBER).order_by("-date_joined")[:limit]:
            rows.append((member.date_joined, _("New member: %(name)s") % {"name": member.first_name or _("member")},
                         reverse("administration:member", args=[member.pk])))
    if manual_payments.can_view(user) and can["membership_payments"]:
        from apps.payments.models import ManualMembershipPayment

        for payment in ManualMembershipPayment.objects.order_by("-created_at")[:limit]:
            rows.append((payment.created_at, _("Membership payment %(ref)s: TZS %(amount)s")
                         % {"ref": payment.reference, "amount": f"{payment.amount:,}"},
                         reverse("administration:membership_payment", args=[payment.pk])))
    if can["withdrawal_requests"]:
        from apps.withdrawals.models import WithdrawalRequest

        for withdrawal in WithdrawalRequest.objects.order_by("-created_at")[:limit]:
            rows.append((withdrawal.created_at, _("Withdrawal request %(ref)s: TZS %(amount)s")
                         % {"ref": withdrawal.reference, "amount": f"{withdrawal.amount:,}"},
                         reverse("administration:withdrawal_request", args=[withdrawal.pk])))
    rows.sort(key=lambda row: row[0], reverse=True)
    return [{"when": when, "text": text, "url": url} for when, text, url in rows[:limit]]


def _alerts(can):
    """Short, actionable problems. Details live on System health."""
    alerts = []
    if can.get("operations"):
        jobs = summary._jobs()
        if jobs["status"] != summary.HEALTHY:
            alerts.append({"text": _("Background jobs are not running, so notifications, reward clearance and "
                                     "checks are waiting."), "url": _url("health"), "action": _("See system health")})
    return alerts


def dashboard(user):
    can = summary.permissions(user)
    return {
        "can": can, "stats": _stats(user, can), "tasks": _tasks(user, can), "money": _money(user, can),
        "activity": _activity(user, can), "alerts": _alerts(can), "now": timezone.now(),
    }
