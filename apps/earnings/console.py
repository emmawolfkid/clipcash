"""FIN-01 administration console pages: Video rewards, campaign funding and earning limits.

Every page enforces its own permission; actions go through apps.earnings services (audited,
four-eyes where money is confirmed or a rate is approved)."""

import datetime

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.administration.views import _error_text, console, console_links
from apps.advertising.models import Campaign, Organization
from apps.memberships.models import MembershipPlan
from apps.rewards import services as rewards
from apps.rewards.models import RewardPolicy
from apps.rewards.terms import eligible_plan_ids

from . import funding, rules, services
from .models import CampaignDeposit, FundingMovement, PlanEarningLimit, SessionReward

REWARD_CAMPAIGN_STATES = rewards.POLICY_CAMPAIGN_STATES


def can_open_video_rewards(user):
    return services.can_view(user) or rewards.can_use_rewards_console(user) or funding.can_view_funding(user)


def _context(request, nav, **extra):
    return {"can": console_links(request.user), "nav": nav, "rewards_active": services.active(), **extra}


def _campaign_row(campaign, plan_names):
    policy = rewards.current_policy(campaign)
    summary = funding.campaign_summary(campaign, policy if policy and policy.kind == RewardPolicy.Kind.TZS else None)
    draft = (RewardPolicy.objects.filter(campaign=campaign, status=RewardPolicy.Status.DRAFT)
             .order_by("-version").first())
    if policy is None:
        state = "none"
    elif policy.kind == RewardPolicy.Kind.CREDITS:
        state = "credits"
    elif not services.active():
        state = "inactive"
    elif summary["remaining"] < policy.reward_amount:
        state = "unfunded"
    else:
        state = "active"
    plan_ids = eligible_plan_ids(campaign)
    return {"campaign": campaign, "policy": policy, "draft": draft, "funding": summary, "state": state,
            "plans": sorted(plan_names[pk] for pk in plan_ids if pk in plan_names)}


@console
class VideoRewardsView(View):
    """One operational workspace: every sponsored campaign (video) with its reward type and rate,
    eligible plans, funding, obligations, approval and activation status."""

    def get(self, request):
        if not can_open_video_rewards(request.user):
            raise PermissionDenied
        query = request.GET.get("q", "").strip()[:100]
        campaigns = (Campaign.objects.select_related("organization", "video").prefetch_related("credit_terms")
                     .filter(status__in=REWARD_CAMPAIGN_STATES).order_by("name"))
        if query:
            campaigns = campaigns.filter(Q(name__icontains=query) | Q(organization__display_name__icontains=query)
                                         | Q(video__title__icontains=query))
        page = Paginator(campaigns, 20).get_page(request.GET.get("page"))
        plan_names = dict(MembershipPlan.objects.values_list("pk", "name"))
        rows = [_campaign_row(campaign, plan_names) for campaign in page.object_list]
        totals = SessionReward.objects.aggregate(
            pending=Coalesce(Sum("amount", filter=Q(status="PENDING")), 0),
            available=Coalesce(Sum("amount", filter=Q(status__in=["AVAILABLE", "RECOVERY_REQUIRED"])), 0),
            reserved=Coalesce(Sum("amount", filter=Q(status="RESERVED")), 0))
        return render(request, "administration/earnings/video_rewards.html", _context(
            request, "video_rewards", rows=rows, page=page, query=query, totals=totals,
            settings_row=services.earning_settings(),
            held=SessionReward.objects.filter(status="PENDING", on_hold=True).count(),
            recovery=SessionReward.objects.filter(status="RECOVERY_REQUIRED").count()))


@console
class VideoRewardView(View):
    """One campaign's reward: configure (draft TZS or credits rate, approved by someone else),
    fund (commit or release verified funds), and manage its rewards (hold, reverse)."""

    def _campaign(self, pk):
        return get_object_or_404(Campaign.objects.select_related("organization", "video"), pk=pk)

    def get(self, request, pk):
        if not can_open_video_rewards(request.user):
            raise PermissionDenied
        campaign = self._campaign(pk)
        plan_names = dict(MembershipPlan.objects.values_list("pk", "name"))
        row = _campaign_row(campaign, plan_names)
        status = request.GET.get("status", "")
        reward_qs = SessionReward.objects.filter(campaign=campaign).select_related("member").order_by("-reserved_at")
        if status in SessionReward.Status.values:
            reward_qs = reward_qs.filter(status=status)
        return render(request, "administration/earnings/video_reward.html", _context(
            request, "video_rewards", row=row, campaign=campaign,
            policies=RewardPolicy.objects.filter(campaign=campaign).order_by("-version")[:10],
            movements=FundingMovement.objects.filter(campaign=campaign).select_related("actor")[:20],
            rewards_page=Paginator(reward_qs, 25).get_page(request.GET.get("page")), status=status,
            statuses=SessionReward.Status.choices,
            org_balance=funding.organization_balance(campaign.organization),
            can_configure=rewards.can_manage_policies(request.user),
            can_allocate=funding.can_allocate(request.user),
            can_manage_rewards=services.can_manage_rewards(request.user),
            reason_message=services.reason_message))

    def post(self, request, pk):
        campaign = self._campaign(pk)
        action = request.POST.get("action", "")
        try:
            if action == "draft":
                raw = request.POST.get("reward_amount", "").strip()
                if not raw.isdigit():
                    raise ValidationError({"reward_amount": _("Enter a whole number.")})
                policy = rewards.create_policy(request.user, campaign, reward_amount=int(raw),
                                               kind=request.POST.get("kind", ""), notes=request.POST.get("notes", ""))
                messages.success(request, _("Reward draft saved. A different administrator must approve it."))
                return redirect("administration:reward_policy", pk=policy.pk)
            if action == "commit":
                funding.commit_funding(request.user, campaign, amount=request.POST.get("amount", "").strip(),
                                       note=request.POST.get("note", ""))
                messages.success(request, _("Verified funds committed to this campaign."))
            elif action == "release":
                funding.release_funding(request.user, campaign, amount=request.POST.get("amount", "").strip(),
                                        reason=request.POST.get("reason", ""))
                messages.success(request, _("Unused funding released to the organization."))
            elif action in ("hold", "unhold", "reverse"):
                reward = get_object_or_404(SessionReward, pk=request.POST.get("reward"), campaign=campaign)
                if action == "reverse":
                    services.staff_reverse(request.user, reward, reason=request.POST.get("reason", ""))
                    messages.success(request, _("Reward reversed."))
                else:
                    services.set_hold(request.user, reward, hold=action == "hold", reason=request.POST.get("reason", ""))
                    messages.success(request, _("Reward updated."))
            else:
                raise PermissionDenied
        except (rewards.NotAuthorized, services.NotAuthorized):
            raise PermissionDenied from None
        except (ValidationError, rewards.RewardsError, services.EarningsError) as error:
            messages.error(request, _error_text(error))
        return redirect("administration:video_reward", pk=pk)


@console
class FundingView(View):
    def get(self, request):
        if not funding.can_view_funding(request.user):
            raise PermissionDenied
        status = request.GET.get("status", "")
        deposits = CampaignDeposit.objects.select_related("organization", "recorded_by", "decided_by")
        if status in CampaignDeposit.Status.values:
            deposits = deposits.filter(status=status)
        organizations = Organization.objects.filter(verification_status=Organization.Verification.APPROVED).order_by(
            "display_name")
        balances = [{"organization": org, "balance": funding.organization_balance(org),
                     "pending": org.deposits.filter(status="RECORDED").count()} for org in organizations[:100]]
        return render(request, "administration/earnings/funding.html", _context(
            request, "campaign_funding", page=Paginator(deposits, 25).get_page(request.GET.get("page")),
            status=status, statuses=CampaignDeposit.Status.choices, methods=CampaignDeposit.Method.choices,
            organizations=organizations, balances=balances, can_record=funding.can_record(request.user),
            today=datetime.date.today().isoformat()))

    def post(self, request):
        organization = Organization.objects.filter(pk=request.POST.get("organization") or None).first()
        try:
            paid_on = datetime.date.fromisoformat(request.POST.get("paid_on", ""))
        except ValueError:
            paid_on = None
        try:
            deposit = funding.record_deposit(
                request.user, organization=organization, method=request.POST.get("method", ""),
                amount=request.POST.get("amount", "").strip().replace(",", ""),
                external_reference=request.POST.get("external_reference", ""), paid_on=paid_on,
                payer_name=request.POST.get("payer_name", ""), note=request.POST.get("note", ""))
            messages.success(request, _("Deposit recorded. A different person must verify the receipt before it "
                                        "can fund campaigns."))
            return redirect("administration:campaign_deposit", pk=deposit.pk)
        except services.NotAuthorized:
            raise PermissionDenied from None
        except ValidationError as error:
            messages.error(request, _error_text(error))
        return redirect("administration:campaign_funding")


@console
class DepositView(View):
    def get(self, request, pk):
        if not funding.can_view_funding(request.user):
            raise PermissionDenied
        deposit = get_object_or_404(CampaignDeposit.objects.select_related("organization", "recorded_by",
                                                                           "decided_by"), pk=pk)
        return render(request, "administration/earnings/deposit.html", _context(
            request, "campaign_funding", deposit=deposit,
            can_decide=funding.can_verify(request.user) and deposit.recorded_by_id != request.user.pk,
            own=deposit.recorded_by_id == request.user.pk))

    def post(self, request, pk):
        deposit = get_object_or_404(CampaignDeposit, pk=pk)
        action = request.POST.get("action", "")
        expected = request.POST.get("expected_status", "")
        try:
            if action == "verify":
                funding.verify_deposit(request.user, deposit, receipt_confirmed=request.POST.get("receipt_confirmed")
                                       == "on", expected_status=expected)
                messages.success(request, _("Receipt verified. The funds are available to commit to campaigns."))
            elif action == "reject":
                funding.reject_deposit(request.user, deposit, reason=request.POST.get("reason", ""),
                                       expected_status=expected)
                messages.success(request, _("Deposit rejected. Nothing was posted."))
            else:
                raise PermissionDenied
        except services.NotAuthorized as error:
            if error.code == "four_eyes":
                messages.error(request, _error_text(error))
                return redirect("administration:campaign_deposit", pk=pk)
            raise PermissionDenied from None
        except ValidationError as error:
            messages.error(request, _error_text(error))
        return redirect("administration:campaign_deposit", pk=pk)


@console
class EarningRulesView(View):
    def get(self, request):
        if not (services.can_view(request.user) or services.can_manage_rules(request.user)):
            raise PermissionDenied
        plans = MembershipPlan.objects.filter(status=MembershipPlan.Status.ACTIVE).order_by("display_order", "name")
        limits = {row.plan_id: row for row in PlanEarningLimit.objects.all()}
        settings_row = services.earning_settings()
        plan_rows = [{"plan": plan, "limit": limits.get(plan.pk), "effective": services.limits_for(plan.pk)}
                     for plan in plans]
        return render(request, "administration/earnings/rules.html", _context(
            request, "earning_rules", settings_row=settings_row, plan_rows=plan_rows,
            can_manage=services.can_manage_rules(request.user)))

    def post(self, request):
        try:
            if request.POST.get("action") == "plan":
                plan = get_object_or_404(MembershipPlan, pk=request.POST.get("plan"))
                rules.set_plan_limit(request.user, plan, daily_amount_limit=request.POST.get("daily_amount_limit"),
                                     daily_view_limit=request.POST.get("daily_view_limit"))
            else:
                rules.update_settings(request.user, expected_version=request.POST.get("expected_version", ""),
                                      daily_amount_limit=request.POST.get("daily_amount_limit"),
                                      daily_view_limit=request.POST.get("daily_view_limit"),
                                      clearance_hours=request.POST.get("clearance_hours"))
            messages.success(request, _("Earning rules saved. They apply to offers made from now on."))
        except services.NotAuthorized:
            raise PermissionDenied from None
        except ValidationError as error:
            messages.error(request, _error_text(error))
        return redirect("administration:earning_rules")


__all__ = ["VideoRewardsView", "VideoRewardView", "FundingView", "DepositView", "EarningRulesView"]
