"""
Member and advertiser payment pages (SANDBOX). Every lookup is scoped to the
signed-in user (own membership payments; own advertiser's deposits). Browser
forms never carry a payment status, and members cannot simulate payments.
"""

import uuid

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.accounts.access import VerifiedAccountRequiredMixin
from apps.advertising import services as advertising
from apps.memberships.models import Subscription

from . import services
from .models import MembershipActivationPreview, PaymentIntent, SandboxCampaignAllocation
from .providers import PaymentsDisabled, sandbox_active


def _errors(error):
    return " ".join(getattr(error, "messages", [str(error)]))


class SandboxRequiredMixin:
    def dispatch(self, request, *args, **kwargs):
        if not sandbox_active():
            raise Http404
        return super().dispatch(request, *args, **kwargs)


class MemberPaymentsView(VerifiedAccountRequiredMixin, SandboxRequiredMixin, View):
    def get(self, request):
        return render(request, "payments/history.html", {
            "nav": "payments", "intents": services.member_intents(request.user)[:50],
            "payable": services.payable_subscriptions(request.user), "form_token": uuid.uuid4().hex})


class StartMembershipPaymentView(VerifiedAccountRequiredMixin, SandboxRequiredMixin, View):
    def post(self, request, subscription_id):
        subscription = get_object_or_404(Subscription, pk=subscription_id, member=request.user)
        try:
            intent = services.create_membership_intent(request.user, subscription,
                                                       idempotency_key=request.POST.get("form_token", ""))
        except services.NotAuthorized:
            raise PermissionDenied from None
        except (ValidationError, services.PaymentError) as error:
            messages.error(request, _errors(error))
            return redirect("payments:history")
        return redirect("payments:intent", intent_id=intent.pk)


class IntentView(VerifiedAccountRequiredMixin, SandboxRequiredMixin, View):
    def get(self, request, intent_id):
        try:
            intent = services.intent_for(request.user, intent_id)
        except services.NotFound:
            raise Http404 from None
        return render(request, "payments/intent.html", {
            "nav": "payments", "intent": intent,
            "preview": MembershipActivationPreview.objects.filter(intent=intent).first(),
            "events": intent.provider_events.order_by("received_at")[:20]})

    def post(self, request, intent_id):
        try:
            intent = services.intent_for(request.user, intent_id)
            services.cancel_intent(request.user, intent)
            messages.success(request, _("Payment cancelled."))
        except services.NotFound:
            raise Http404 from None
        except services.NotAuthorized:
            raise PermissionDenied from None
        except services.PaymentError as error:
            messages.error(request, str(error))
        return redirect("payments:intent", intent_id=intent_id)


class AdvertiserFundingView(VerifiedAccountRequiredMixin, SandboxRequiredMixin, View):
    template_name = "payments/funding.html"

    def _organization(self, request, organization_id):
        try:
            return advertising.organization_for(request.user, organization_id)
        except advertising.NotFound:
            raise Http404 from None

    def get(self, request, organization_id):
        organization = self._organization(request, organization_id)
        membership = advertising.membership_for(request.user, organization)
        return render(request, self.template_name, {
            "nav": "advertiser", "tab": "funding", "organization": organization, "membership": membership,
            "is_owner": membership.role == membership.Role.OWNER, "funds": services.funds_for(organization),
            "deposits": PaymentIntent.objects.filter(organization=organization).order_by("-created_at")[:50],
            "allocations": SandboxCampaignAllocation.objects.select_related("campaign")
                           .filter(organization=organization).order_by("-created_at")[:50],
            "campaigns": organization.campaigns.filter(status__in=services.ALLOCATABLE_CAMPAIGN_STATES),
            "form_token": uuid.uuid4().hex})

    def post(self, request, organization_id):
        organization = self._organization(request, organization_id)
        action = request.POST.get("action", "")
        try:
            if action == "deposit":
                intent = services.create_deposit_intent(request.user, organization,
                                                        amount=_whole(request.POST.get("amount")),
                                                        idempotency_key=request.POST.get("form_token", ""))
                return redirect("payments:intent", intent_id=intent.pk)
            if action == "allocate":
                campaign = get_object_or_404(organization.campaigns, pk=request.POST.get("campaign"))
                services.allocate(request.user, organization, campaign, amount=_whole(request.POST.get("amount")))
                messages.success(request, _("Sandbox funds allocated."))
            elif action == "release":
                allocation = get_object_or_404(SandboxCampaignAllocation, pk=request.POST.get("allocation"),
                                               organization=organization)
                services.release_allocation(request.user, allocation)
                messages.success(request, _("Allocation released."))
            else:
                raise Http404
        except services.NotAuthorized:
            raise PermissionDenied from None
        except (ValidationError, services.PaymentError) as error:
            messages.error(request, _errors(error))
        return redirect("payments:funding", organization_id=organization.pk)


def _whole(raw):
    raw = (raw or "").strip().replace(",", "")
    if not raw.isdigit():
        raise ValidationError({"amount": "Enter a whole number of shillings."})
    return int(raw)


@method_decorator(csrf_exempt, name="dispatch")
class SandboxCallbackView(View):
    """Provider-to-server notice: authenticated by HMAC signature, not by browser CSRF."""

    def post(self, request):
        try:
            status, outcome = services.receive_callback("sandbox", request.body,
                                                        request.headers.get("X-Sandbox-Signature", ""))
        except PaymentsDisabled:
            raise Http404 from None
        return JsonResponse({"outcome": str(outcome)}, status=status)
