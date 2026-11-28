"""REF-01 administration console: referral overview and management, programme rules and the
marketing budget. Every page checks its own permission; every action goes through
apps.referrals.services or apps.referrals.funding (no amount is ever chosen by staff)."""

import datetime

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.accounts.lookup import identifier_q
from apps.administration.views import _error_text, console, console_links
from apps.audit.models import AuditEvent
from apps.wallets.models import WalletTransaction

from . import funding, services
from .models import MarketingDeposit, Referral, ReferralPolicy, ReferralReward

R = Referral.Status


def _ctx(request, nav, **extra):
    return {"can": console_links(request.user), "nav": nav, "rewards_active": services.rewards_active(),
            "settlement_active": services.settlement_active(), **extra}


def _handle(request, action, target):
    """Run one action; translate refusals into messages. Returns True on success."""
    try:
        action()
        return True
    except services.NotAuthorized as error:
        if error.code == "permission_missing":
            raise PermissionDenied from None
        messages.error(request, _error_text(error))
    except (ValidationError, services.ReferralError) as error:
        messages.error(request, _error_text(error))
    return False


@console
class ReferralListView(View):
    def get(self, request):
        if not services.can_view(request.user):
            raise PermissionDenied
        qs = Referral.objects.select_related("referrer", "referred", "reward").order_by("-created_at")
        filters = {key: request.GET.get(key, "").strip()[:100] for key in ("q", "status", "start", "end")}
        if filters["q"]:
            text = filters["q"]
            qs = qs.filter(Q(reference__iexact=text) | Q(code__iexact=text.replace(" ", ""))
                           | identifier_q(text, "referrer__") | identifier_q(text, "referred__"))
        if filters["status"] in R.values:
            qs = qs.filter(status=filters["status"])
        for key, lookup in (("start", "created_at__date__gte"), ("end", "created_at__date__lte")):
            try:
                if filters[key]:
                    qs = qs.filter(**{lookup: datetime.date.fromisoformat(filters[key])})
            except ValueError:
                filters[key] = ""
        counts = Referral.objects.aggregate(
            total=Count("pk"), awaiting=Count("pk", filter=Q(status=R.AWAITING)),
            review=Count("pk", filter=Q(status=R.UNDER_REVIEW)),
            qualified=Count("pk", filter=Q(status__in=[R.QUALIFIED, R.REWARD_PENDING, R.REWARD_CLEARED])),
            rejected=Count("pk", filter=Q(status__in=[R.REJECTED, R.REVERSED])),
            expired=Count("pk", filter=Q(status=R.EXPIRED)))
        counts["conversion"] = round(100 * counts["qualified"] / counts["total"]) if counts["total"] else 0
        params = request.GET.copy()
        params.pop("page", None)
        return render(request, "administration/referrals/list.html", _ctx(
            request, "referrals", tab="overview", page=Paginator(qs, 25).get_page(request.GET.get("page")), filters=filters,
            page_query=params.urlencode(), statuses=R.choices, counts=counts, budget=funding.budget_summary(),
            policy=services.current_policy()))


@console
class ReferralDetailView(View):
    template = "administration/referrals/detail.html"

    def get(self, request, pk):
        if not services.can_view(request.user):
            raise PermissionDenied
        referral = get_object_or_404(Referral.objects.select_related("referrer", "referred", "policy",
                                                                     "reviewed_by"), pk=pk)
        reward = ReferralReward.objects.filter(referral=referral).first()
        entries = [e for e in ((reward.pending_entry_id, reward.cleared_entry_id, reward.reversal_entry_id)
                               if reward else ()) if e]
        user = request.user
        party = user.pk in (referral.referrer_id, referral.referred_id)
        return render(request, self.template, _ctx(
            request, "referrals", tab="overview", referral=referral, reward=reward, progress=services.progress(referral),
            flags=[(flag, services.RISK_TEXT.get(flag, flag)) for flag in referral.risk_flags],
            explanation=services.explanation(referral),
            ledger=WalletTransaction.objects.filter(journal_entry__in=entries).order_by("created_at"),
            audit=AuditEvent.objects.filter(target_id__in=[str(referral.pk)] + ([str(reward.pk)] if reward else []))
            .order_by("-occurred_at")[:40],
            can_review=services.can_review(user) and not party))

    def post(self, request, pk):
        referral = get_object_or_404(Referral, pk=pk)
        reward = ReferralReward.objects.filter(referral=referral).first()
        action, user = request.POST.get("action", ""), request.user
        note = request.POST.get("note", "")
        actions = {
            "approve": lambda: services.review(user, referral, approve=True, note=note),
            "reject": lambda: services.review(user, referral, approve=False, note=note),
            "hold": lambda: services.set_hold(user, reward, hold=True, reason=note),
            "unhold": lambda: services.set_hold(user, reward, hold=False, reason=note),
            "reverse": lambda: services.staff_reverse(user, reward, reason=note),
        }
        if action not in actions or (action in ("hold", "unhold", "reverse") and reward is None):
            raise PermissionDenied
        if _handle(request, actions[action], referral):
            messages.success(request, _("Referral updated."))
        return redirect("administration:referral", pk=pk)


@console
class ReferralProgrammeView(View):
    def get(self, request):
        user = request.user
        if not services.can_view(user):
            raise PermissionDenied
        return render(request, "administration/referrals/programme.html", _ctx(
            request, "referrals", tab="programme", current=services.current_policy(), proposal=services.pending_proposal(),
            history=ReferralPolicy.objects.select_related("created_by", "decided_by").order_by("-version")[:15],
            can_propose=services.can_manage_policy(user), can_approve=services.can_approve_policy(user),
            budget=funding.budget_summary()))

    def post(self, request):
        user, action = request.user, request.POST.get("action", "")
        if action == "propose":
            values = {name: request.POST.get(name) for name in services.POLICY_FIELDS}
            done = _handle(request, lambda: services.propose_policy(
                user, expected_version=request.POST.get("expected_version", ""),
                reason=request.POST.get("reason", ""), **values), None)
            if done:
                messages.success(request, _("Change proposed. It takes effect when a different person approves it."))
        elif action in ("approve", "reject"):
            proposal = get_object_or_404(ReferralPolicy, pk=request.POST.get("proposal"))
            if _handle(request, lambda: services.decide_policy(user, proposal, approve=action == "approve",
                                                               note=request.POST.get("note", "")), proposal):
                messages.success(request, _("Programme rules updated.") if action == "approve"
                                 else _("Proposal rejected."))
        else:
            raise PermissionDenied
        return redirect("administration:referral_programme")


@console
class ReferralFundingView(View):
    def get(self, request):
        user = request.user
        if not services.can_view(user):
            raise PermissionDenied
        status = request.GET.get("status", "")
        deposits = MarketingDeposit.objects.select_related("recorded_by", "decided_by").order_by("-created_at")
        if status in MarketingDeposit.Status.values:
            deposits = deposits.filter(status=status)
        return render(request, "administration/referrals/funding.html", _ctx(
            request, "referrals", tab="funding", budget=funding.budget_summary(), status=status,
            page=Paginator(deposits, 25).get_page(request.GET.get("page")), statuses=MarketingDeposit.Status.choices,
            methods=MarketingDeposit.Method.choices, today=timezone.localdate().isoformat(),
            can_record=funding.can_record(user), can_verify=funding.can_verify(user),
            can_allocate=funding.can_allocate(user)))

    def post(self, request):
        user, action, post = request.user, request.POST.get("action", ""), request.POST
        if action == "record":
            def run():
                try:
                    paid_on = datetime.date.fromisoformat(post.get("paid_on", ""))
                except ValueError:
                    paid_on = None
                funding.record_deposit(user, method=post.get("method", ""), amount=post.get("amount", ""),
                                       external_reference=post.get("external_reference", ""), paid_on=paid_on,
                                       note=post.get("note", ""))
        elif action in ("verify", "reject_deposit"):
            deposit = get_object_or_404(MarketingDeposit, pk=post.get("deposit"))
            if action == "verify":
                def run():
                    funding.verify_deposit(user, deposit, receipt_confirmed=post.get("receipt_confirmed") == "on")
            else:
                def run():
                    funding.reject_deposit(user, deposit, reason=post.get("reason", ""))
        elif action == "commit":
            def run():
                funding.commit_budget(user, amount=post.get("amount", ""), note=post.get("note", ""))
        elif action == "release":
            def run():
                funding.release_budget(user, amount=post.get("amount", ""), reason=post.get("reason", ""))
        else:
            raise PermissionDenied
        if _handle(request, run, None):
            messages.success(request, _("Referral budget updated."))
        return redirect("administration:referral_funding")
