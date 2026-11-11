"""
Member and advertiser payment pages (SANDBOX), plus the PAY-01 manual membership
payment pages (REAL Airtel Lipa payments, verified by staff; apps.payments.manual).
Every lookup is scoped to the signed-in user (own membership payments; own
advertiser's deposits). Browser forms never carry a payment status, and members
cannot simulate, approve or activate anything.
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

from . import manual, services
from .forms import ManualPaymentForm
from .models import ManualMembershipPayment, MembershipActivationPreview, PaymentIntent, SandboxCampaignAllocation
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


# --- PAY-01: manual membership payments (Airtel Lipa), REAL money -------------------------


class ManualPaymentsRequiredMixin:
    """404 unless manual membership payments are enabled in this deployment."""

    def dispatch(self, request, *args, **kwargs):
        if not manual.manual_payments_enabled():
            raise Http404
        return super().dispatch(request, *args, **kwargs)


MANUAL_STATE_MESSAGES = {
    "already_submitted": _("A payment for this membership is already waiting for verification."),
    "not_pending": _("This membership is not waiting for payment."),
    "choice_expired": _("This membership choice has expired. Choose the plan again to pay."),
    "plan_unavailable": _("This plan is no longer available. Choose another plan."),
    "no_instructions": _("Payments are temporarily unavailable. Please try again later."),
    "payments_disabled": _("Payments are temporarily unavailable. Please try again later."),
    "rate_limited": _("Too many payment submissions. Please wait an hour and try again."),
}


class ManualPayView(VerifiedAccountRequiredMixin, ManualPaymentsRequiredMixin, View):
    """Plan, exact amount, Airtel Lipa details and the form to submit payment details."""

    template_name = "payments/manual_pay.html"

    def _subscription(self, request, subscription_id):
        return get_object_or_404(Subscription.objects.select_related("plan"), pk=subscription_id,
                                 member=request.user)

    def _render(self, request, subscription, form, status=200):
        return render(request, self.template_name, {
            "nav": "membership", "subscription": subscription, "form": form,
            "instruction": manual.active_instruction(),
            "previous": subscription.manual_payments.exclude(status=ManualMembershipPayment.Status.PENDING_VERIFICATION)
                                                    .order_by("-created_at")[:10],
            "breadcrumbs": [("My membership", "memberships:mine")]}, status=status)

    def get(self, request, subscription_id):
        subscription = self._subscription(request, subscription_id)
        claim = manual.open_claim(subscription)
        if claim is not None:
            return redirect("payments:manual_claim", pk=claim.pk)
        if subscription.status != Subscription.Status.PENDING:
            messages.info(request, MANUAL_STATE_MESSAGES["not_pending"])
            return redirect("memberships:mine")
        return self._render(request, subscription, ManualPaymentForm())

    def post(self, request, subscription_id):
        subscription = self._subscription(request, subscription_id)
        form = ManualPaymentForm(request.POST)
        if not form.is_valid():
            return self._render(request, subscription, form, status=400)
        try:
            claim, _created = manual.submit_claim(request.user, subscription, **form.cleaned_data)
        except ValidationError as error:
            _form_errors(form, error)
            return self._render(request, subscription, form, status=400)
        except manual.NotAuthorized:
            raise PermissionDenied from None
        except manual.ManualPaymentError as error:
            messages.error(request, MANUAL_STATE_MESSAGES.get(error.code, _("The payment could not be submitted.")))
            if error.code == "already_submitted":
                return redirect("payments:manual_pay", subscription_id=subscription.pk)
            if error.code in ("no_instructions", "rate_limited"):
                return self._render(request, subscription, form, status=429 if error.code == "rate_limited" else 503)
            return redirect("memberships:mine")
        messages.success(request, _("Thank you. Your payment details were submitted and are waiting for verification."))
        return redirect("payments:manual_claim", pk=claim.pk)


class ManualClaimView(VerifiedAccountRequiredMixin, ManualPaymentsRequiredMixin, View):
    """Status of one of the member's own payment submissions."""

    def _claim(self, request, pk):
        return get_object_or_404(ManualMembershipPayment.objects.select_related("subscription", "instruction"),
                                 pk=pk, member=request.user)

    def get(self, request, pk):
        claim = self._claim(request, pk)
        subscription = claim.subscription
        can_resubmit = (claim.status in (ManualMembershipPayment.Status.REJECTED,
                                         ManualMembershipPayment.Status.CANCELLED)
                        and subscription.status == Subscription.Status.PENDING
                        and manual.open_claim(subscription) is None)
        return render(request, "payments/manual_claim.html", {
            "nav": "membership", "claim": claim, "subscription": subscription, "can_resubmit": can_resubmit,
            "breadcrumbs": [("My membership", "memberships:mine")]})

    def post(self, request, pk):
        claim = self._claim(request, pk)
        if request.POST.get("action") != "cancel":
            raise Http404
        try:
            manual.cancel_claim(request.user, claim)
            messages.success(request, _("Your payment details were withdrawn."))
        except manual.NotAuthorized:
            raise PermissionDenied from None
        except manual.ManualPaymentError:
            messages.error(request, _("These payment details can no longer be withdrawn."))
        return redirect("payments:manual_claim", pk=claim.pk)


def _form_errors(form, error):
    if hasattr(error, "error_dict"):
        for field, field_messages in error.message_dict.items():
            form.add_error(field if field in form.fields else None, field_messages)
    else:
        form.add_error(None, error.messages)


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
