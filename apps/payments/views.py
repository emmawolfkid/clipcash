"""
Member and advertiser payment pages (SANDBOX), plus the PAY-01 manual membership
payment pages (REAL Airtel Lipa payments, verified by staff; apps.payments.manual).
Every lookup is scoped to the signed-in user (own membership payments; own
advertiser's deposits). Browser forms never carry a payment status, and members
cannot simulate, approve or activate anything.
"""

import uuid

from django.conf import settings
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
from apps.core import features
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
    "already_submitted": _("Verification is already requested for this membership. We are checking your payment."),
    "not_pending": _("This membership is not waiting for payment."),
    "choice_expired": _("This membership choice has expired. Choose the plan again to pay."),
    "plan_unavailable": _("This plan is no longer available. Choose another plan."),
    # PAY-LIVE-01C: the one "purchases closed" explanation members see everywhere.
    "no_instructions": manual.PURCHASE_CLOSED_MESSAGE,
    "payments_disabled": manual.PURCHASE_CLOSED_MESSAGE,
    "rate_limited": _("Too many payment submissions. Please wait an hour and try again."),
}


class ManualPayView(VerifiedAccountRequiredMixin, View):
    """Plan, exact amount, Airtel Lipa details and the form to submit payment details.

    When manual payments are switched off, or no Lipa details are published, the member's
    own pending choice still shows, with a clear message and no form; a POST records
    nothing (and the service refuses it too)."""

    template_name = "payments/manual_pay.html"

    def dispatch(self, request, *args, **kwargs):
        if not features.is_enabled("memberships"):
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def _subscription(self, request, subscription_id):
        return get_object_or_404(Subscription.objects.select_related("plan"), pk=subscription_id,
                                 member=request.user)

    def _quote(self, request, subscription):
        from .models import ManualPaymentQuote
        key = request.POST.get("quote") or request.GET.get("quote")
        if key:
            try:
                return get_object_or_404(ManualPaymentQuote.objects.select_related("instruction"),
                                         pk=key, subscription=subscription)
            except (ValueError, ValidationError):
                raise Http404 from None
        return None

    def _render(self, request, subscription, form, status=200, quote=None):
        enabled = manual.manual_payments_enabled()
        quote = (quote or self._quote(request, subscription)) if enabled else None
        methods = manual.active_instructions() if enabled else []
        if enabled and quote is None and len(methods) == 1 and subscription.status == Subscription.Status.PENDING:
            try:
                quote = manual.issue_instructions(request.user, subscription, methods[0])
            except manual.ManualPaymentError:
                pass
        if quote is not None and not form.is_bound:
            form.initial["quote"] = quote.pk
        if quote is not None and quote.instruction.is_bank:  # PRD-04: a bank transfer has no paying phone.
            form.fields["payer_phone"].label = _("Your phone number (optional)")
            form.fields["payer_phone"].help_text = _("So our team can contact you about this bank transfer. The "
                                                     "transfer itself is checked in the bank's records.")
        if not form.is_bound and request.user.phone_number:
            # REG-03: a convenience only. Every current method is mobile money, so the number paid
            # from is asked here, at checkout; accounts without a number (username or email only)
            # simply type it, and members who paid from another number change it.
            form.initial.setdefault("payer_phone", request.user.phone_number)
        return render(request, self.template_name, {
            "nav": "membership", "subscription": subscription, "form": form, "payments_enabled": enabled,
            "instruction": quote.instruction if quote else None, "quote": quote, "methods": methods,
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
        quote = self._quote(request, subscription)
        status = 200 if manual.manual_payments_enabled() and (quote or manual.manual_payments_ready()) else 503
        return self._render(request, subscription, ManualPaymentForm(), status=status, quote=quote)

    def post(self, request, subscription_id):
        subscription = self._subscription(request, subscription_id)
        if request.POST.get("action") == "choose_method":
            from .models import ManualPaymentInstruction
            try:
                instruction = get_object_or_404(ManualPaymentInstruction, pk=request.POST.get("instruction"), is_active=True)
            except (ValueError, ValidationError):
                raise Http404 from None
            try:
                quote = manual.issue_instructions(request.user, subscription, instruction)
            except manual.ManualPaymentError as error:
                messages.error(request, str(error))
                return redirect("payments:manual_pay", subscription_id=subscription.pk)
            from django.urls import reverse
            return redirect(reverse("payments:manual_pay", args=[subscription.pk]) + "?quote=" + str(quote.pk))
        # PRD-04: refuse an oversized upload before reading it (the form fields are small; 64 KB margin).
        try:
            declared = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            declared = 0
        if declared > settings.PAYMENTS_EVIDENCE_MAX_BYTES + 64 * 1024:
            messages.error(request, _("The receipt is too large. The limit is %(mb)s MB. Nothing was submitted.")
                           % {"mb": max(1, settings.PAYMENTS_EVIDENCE_MAX_BYTES // (1024 * 1024))})
            return self._render(request, subscription, ManualPaymentForm(), status=413)
        form = ManualPaymentForm(request.POST, request.FILES)
        quote = self._quote(request, subscription)
        if not manual.manual_payments_enabled() or (quote is None and not manual.manual_payments_ready()):  # Disabled or no Lipa details: nothing is recorded.
            return self._render(request, subscription, form, status=503)
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
        messages.success(request, _("Thank you. Verification requested: your membership starts once our team confirms your payment."))
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
        from apps.memberships.entitlements import label

        return render(request, "payments/manual_claim.html", {
            "nav": "membership", "claim": claim, "subscription": subscription, "can_resubmit": can_resubmit,
            "entitlement_labels": [label(code) for code in subscription.entitlements],
            "breadcrumbs": [("My membership", "memberships:mine")]})

    def post(self, request, pk):
        claim = self._claim(request, pk)
        if request.POST.get("action") != "cancel":
            raise Http404
        try:
            manual.cancel_claim(request.user, claim)
            messages.success(request, _("Your verification request was withdrawn."))
        except manual.NotAuthorized:
            raise PermissionDenied from None
        except manual.ManualPaymentError:
            messages.error(request, _("This verification request can no longer be withdrawn."))
        return redirect("payments:manual_claim", pk=claim.pk)


class ManualEvidenceView(VerifiedAccountRequiredMixin, View):
    """PRD-04: download a claim's receipt evidence. Only the claim's own member and staff who may
    view manual payments; anyone else gets 404 (the claim's existence is not revealed). Always a
    download with a fixed name and type, never rendered inline by the site, never cached. Staff
    downloads are audited."""

    def get(self, request, pk):
        from django.http import FileResponse

        from apps.audit.services import record_business_event

        claim = ManualMembershipPayment.objects.select_related("evidence").filter(pk=pk).first()
        evidence = getattr(claim, "evidence", None) if claim is not None else None
        if evidence is None or not manual.can_download_evidence(request.user, claim):
            raise Http404
        if claim.member_id != request.user.pk:
            record_business_event("membership.manual_payment.evidence_viewed", actor=request.user, target=claim,
                                  metadata={"reference": claim.reference})
        try:
            handle = manual.payment_evidence.open_file(evidence)
        except OSError:
            raise Http404 from None
        response = FileResponse(handle, as_attachment=True, content_type=evidence.content_type,
                                filename=f"payment-evidence-{claim.reference}.{evidence.extension}")
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        response["Content-Security-Policy"] = "sandbox; default-src 'none'"
        return response


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
