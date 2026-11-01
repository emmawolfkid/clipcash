from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView

from apps.accounts.access import VerifiedAccountRequiredMixin
from apps.core import features
from apps.core.features import feature_required

from . import services
from .access import current_subscription
from .entitlements import label
from .models import MembershipPlan, Subscription

MESSAGES = {
    "too_early": _("You can renew in the last %(n)s days of your membership.")
                 % {"n": settings.MEMBERSHIPS_RENEWAL_WINDOW_DAYS},
    "already_renewed": _("You already have a renewal scheduled."),
    "membership_suspended": _("Your membership is suspended. Please contact support."),
    "plan_unavailable": _("This plan is no longer available."),
}


def payment_context():
    return {"payments_available": features.is_enabled("live_payments")}


@method_decorator(feature_required("memberships"), name="dispatch")
class PlanListView(ListView):
    template_name = "memberships/plan_list.html"
    context_object_name = "plans"

    def get_queryset(self):
        return MembershipPlan.objects.filter(status=MembershipPlan.Status.ACTIVE, is_public=True)

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), **payment_context()}


@method_decorator(feature_required("memberships"), name="dispatch")
class PlanDetailView(DetailView):
    template_name = "memberships/plan_detail.html"
    context_object_name = "plan"
    slug_field = "code"
    slug_url_kwarg = "code"

    def get_queryset(self):
        return MembershipPlan.objects.filter(status=MembershipPlan.Status.ACTIVE, is_public=True)

    def get_context_data(self, **kwargs):
        plan = self.object
        return {**super().get_context_data(**kwargs), **payment_context(),
                "entitlement_labels": [label(code) for code in plan.entitlements]}


@method_decorator(feature_required("memberships"), name="dispatch")
class ChoosePlanView(VerifiedAccountRequiredMixin, View):
    """POST only: record the member's choice as a PENDING membership."""

    def post(self, request, code):
        plan = get_object_or_404(MembershipPlan, code=code, status=MembershipPlan.Status.ACTIVE, is_public=True)
        try:
            services.choose_plan(request.user, plan)
            messages.success(request, _("Your membership choice is saved and waiting for payment."))
        except services.MembershipError as error:
            messages.error(request, MESSAGES.get(error.code, _("This membership cannot be selected right now.")))
        return redirect("memberships:mine")


@method_decorator(feature_required("memberships"), name="dispatch")
class MyMembershipView(VerifiedAccountRequiredMixin, TemplateView):
    template_name = "memberships/my_membership.html"

    def get_context_data(self, **kwargs):
        now = timezone.now()
        mine = Subscription.objects.filter(member=self.request.user).select_related("plan")
        current = current_subscription(self.request.user, now)
        queued = mine.filter(status=Subscription.Status.ACTIVE, starts_at__gt=now).order_by("starts_at").first()
        pending = mine.filter(status=Subscription.Status.PENDING).first()
        renewal_opens = None
        if current is not None and queued is None:
            renewal_opens = current.expires_at - timedelta(days=settings.MEMBERSHIPS_RENEWAL_WINDOW_DAYS)
        return {
            **super().get_context_data(**kwargs), **payment_context(),
            "current": current, "queued": queued, "pending": pending,
            "history": mine.order_by("-created_at")[:50],
            "renewal_opens": renewal_opens, "renewal_open_now": renewal_opens is not None and renewal_opens <= now,
            "now": now,
        }


@method_decorator(feature_required("memberships"), name="dispatch")
class SubscriptionDetailView(VerifiedAccountRequiredMixin, DetailView):
    template_name = "memberships/subscription_detail.html"
    context_object_name = "subscription"

    def get_queryset(self):
        # Members only ever see their own records; anything else is a 404.
        return Subscription.objects.filter(member=self.request.user).select_related("plan")

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), **payment_context(),
                "entitlement_labels": [label(code) for code in self.object.entitlements]}


@method_decorator(feature_required("memberships"), name="dispatch")
class CancelPendingView(VerifiedAccountRequiredMixin, View):
    def post(self, request, pk):
        subscription = get_object_or_404(Subscription, pk=pk, member=request.user)
        try:
            services.cancel_pending(request.user, subscription)
            messages.success(request, _("Your pending membership choice was cancelled."))
        except services.MembershipError:
            messages.error(request, _("This membership can no longer be cancelled here."))
        return redirect("memberships:mine")
