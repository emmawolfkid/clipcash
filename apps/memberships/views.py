from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView

from apps.accounts.access import DEMO_UNCONFIRMED_BLOCKED, VerifiedAccountRequiredMixin
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
    "email_unverified": DEMO_UNCONFIRMED_BLOCKED,  # Only approved demo accounts get this far unverified.
    "payment_under_review": _("A payment for your current choice is being verified. You can change plans after "
                              "it is approved or rejected."),
}


def payment_context():
    from apps.payments import manual

    enabled = manual.manual_payments_enabled()
    return {"payments_available": features.is_enabled("live_payments"),
            # PAY-01: real membership payments via Airtel Lipa, verified by staff.
            "manual_payments": enabled, "manual_payments_ready": enabled and manual.manual_payments_ready()}


def manual_payment_state(subscription):
    """The latest manual payment claim for a pending choice (open first), or None."""
    if subscription is None:
        return None
    claims = subscription.manual_payments.order_by("-created_at")
    return claims.filter(status="PENDING_VERIFICATION").first() or claims.first()


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
            subscription = services.choose_plan(request.user, plan)
        except services.MembershipError as error:
            messages.error(request, MESSAGES.get(error.code, _("This membership cannot be selected right now.")))
            return redirect("memberships:mine")
        if payment_context()["manual_payments_ready"]:
            messages.success(request, _("Your plan choice is saved. Pay the amount below, then submit the payment "
                                        "details."))
            return redirect("payments:manual_pay", subscription_id=subscription.pk)
        messages.success(request, _("Your membership choice is saved and waiting for payment."))
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
            "pending_payment": manual_payment_state(pending),
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
        pending = self.object if self.object.status == Subscription.Status.PENDING else None
        return {**super().get_context_data(**kwargs), **payment_context(),
                "pending_payment": manual_payment_state(pending),
                "entitlement_labels": [label(code) for code in self.object.entitlements]}


@method_decorator(feature_required("memberships"), name="dispatch")
class CancelPendingView(VerifiedAccountRequiredMixin, View):
    def post(self, request, pk):
        subscription = get_object_or_404(Subscription, pk=pk, member=request.user)
        try:
            services.cancel_pending(request.user, subscription)
            messages.success(request, _("Your pending membership choice was cancelled."))
        except services.MembershipError as error:
            messages.error(request, MESSAGES["payment_under_review"] if error.code == "payment_under_review"
                           else _("This membership can no longer be cancelled here."))
        return redirect("memberships:mine")
