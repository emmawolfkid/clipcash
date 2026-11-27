"""
Member withdrawal pages (/withdrawals/). FIN-02: real withdrawals (when switched on): review the
fee and net payout, confirm, track and cancel; payout destinations (masked; a new one needs the
password confirmed again); the withdrawal identity check. DEMO scenarios are listed separately.
Every lookup is scoped to the signed-in member.
"""

import secrets

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_post_parameters

from apps.accounts import reauth
from apps.accounts.access import DEMO_UNCONFIRMED_BLOCKED, VerifiedAccountRequiredMixin, demo_unconfirmed
from apps.payments.providers import PaymentsDisabled, sandbox_active
from apps.wallets import wallet_services

from . import protect, real, services
from .models import DemoWithdrawal, PayoutBeneficiary, WithdrawalIdentity, WithdrawalRequest


def _errors(error):
    return " ".join(getattr(error, "messages", [str(error)]))


class OverviewView(VerifiedAccountRequiredMixin, View):
    def get(self, request):
        preview = review = None
        raw = request.GET.get("preview_amount", "").strip()
        if raw:
            try:
                preview = services.demo_preview(request.user, amount=int(raw) if raw.isdigit() else raw)
            except (ValidationError, ValueError) as error:
                messages.error(request, _errors(error))
        payable = PayoutBeneficiary.objects.filter(member=request.user, status="ACTIVE").exclude(identifier_encrypted="")
        amount_raw = request.GET.get("amount", "").strip().replace(",", "")
        if amount_raw and real.requests_active():  # FIN-02 review step: nothing is reserved yet.
            beneficiary = payable.filter(pk=request.GET.get("destination") or None).first()
            try:
                decision = services.withdrawal_eligibility(request.user, amount=int(amount_raw) if amount_raw.isdigit()
                                                           else amount_raw, beneficiary=beneficiary,
                                                           require_destination=True)
                review = {"decision": decision, "beneficiary": beneficiary, "token": secrets.token_urlsafe(24),
                          "messages": [services.ELIGIBILITY_MESSAGES.get(c, c) for c in decision.reasons]}
            except (ValidationError, ValueError) as error:
                messages.error(request, _errors(error))
        eligibility = services.withdrawal_eligibility(request.user)
        wallet = wallet_services.wallet_for(request.user)
        return render(request, "withdrawals/overview.html", {
            "nav": "withdrawals", "eligibility": eligibility, "active": real.requests_active(),
            "messages_for": [services.ELIGIBILITY_MESSAGES.get(c, c) for c in eligibility.reasons],
            "wallet": wallet, "balances": wallet_services.balances(wallet) if wallet else None,
            "beneficiaries": PayoutBeneficiary.objects.filter(member=request.user, status="ACTIVE"),
            "payable": payable, "settings_row": eligibility.settings, "review": review,
            "requests": WithdrawalRequest.objects.filter(member=request.user)[:20],
            "identity": real.identity_of(request.user),
            "demos": services.demo_for_member(request.user)[:20], "preview": preview,
            "sandbox": sandbox_active()})


class RequestView(VerifiedAccountRequiredMixin, View):
    """FIN-02: confirm a reviewed withdrawal (reserves the wallet deduction)."""

    def post(self, request):
        try:
            withdrawal = real.submit(request.user, beneficiary_id=request.POST.get("destination") or None,
                                     amount=request.POST.get("amount", "").strip(), token=request.POST.get("token", ""))
            messages.success(request, _("Withdrawal requested. The amount is reserved until it is paid or released."))
            return redirect("withdrawals:request", withdrawal.pk)
        except (ValidationError, real.WithdrawalFlowError) as error:
            messages.error(request, _errors(error))
        return redirect("withdrawals:overview")


class RequestDetailView(VerifiedAccountRequiredMixin, View):
    def get(self, request, withdrawal_id):
        withdrawal = get_object_or_404(WithdrawalRequest, pk=withdrawal_id, member=request.user)
        labels = dict(WithdrawalRequest.Status.choices)
        events = list(withdrawal.events.all())
        for event in events:
            event.status_label = labels.get(event.to_status, "")
        return render(request, "withdrawals/request.html", {
            "nav": "withdrawals", "withdrawal": withdrawal, "events": events,
            "reason": real.REASON_MESSAGES.get(withdrawal.reason_code, "")})

    def post(self, request, withdrawal_id):
        withdrawal = get_object_or_404(WithdrawalRequest, pk=withdrawal_id, member=request.user)
        try:
            real.cancel(request.user, withdrawal)
            messages.success(request, _("Withdrawal cancelled. The amount is back in your available balance."))
        except real.WithdrawalFlowError as error:
            messages.error(request, _errors(error))
        return redirect("withdrawals:request", withdrawal.pk)


@method_decorator([never_cache, sensitive_post_parameters("id_number")], name="dispatch")
class IdentityView(VerifiedAccountRequiredMixin, View):
    """FIN-02: the identity check needed for withdrawals (separate from registration)."""

    def get(self, request):
        return render(request, "withdrawals/identity.html", {
            "nav": "withdrawals", "identity": real.identity_of(request.user),
            "id_types": WithdrawalIdentity.IdType.choices, "protected": protect.available()})

    def post(self, request):
        try:
            real.submit_identity(request.user, legal_name=request.POST.get("legal_name", ""),
                                 id_type=request.POST.get("id_type", ""), id_number=request.POST.get("id_number", ""))
            messages.success(request, _("Identity details sent. ClipCash staff will check them."))
            return redirect("withdrawals:overview")
        except protect.ProtectionUnavailable:
            messages.error(request, _("Identity checks are not available on this site yet."))
        except (ValidationError, real.WithdrawalFlowError) as error:
            messages.error(request, _errors(error))
        return redirect("withdrawals:identity")


@method_decorator(sensitive_post_parameters("identifier", "current_password"), name="dispatch")
class DestinationsView(VerifiedAccountRequiredMixin, View):
    def get(self, request):
        return render(request, "withdrawals/destinations.html", {
            "nav": "withdrawals", "beneficiaries": PayoutBeneficiary.objects.filter(member=request.user)
                                                                        .order_by("status", "-created_at"),
            "networks": PayoutBeneficiary.Network.choices, "sandbox": sandbox_active(),
            "banks": sorted(real.TANZANIA_BANKS.items(), key=lambda item: item[1]),
            "require_password": not reauth.is_fresh(request, request.user)})

    def post(self, request):
        action = request.POST.get("action", "")
        try:
            if action == "add":
                # FIN-02: a new payout destination needs the password confirmed again (apps.accounts.reauth).
                if services._member_ready(request.user) and not reauth.is_fresh(request, request.user):
                    if not request.user.check_password(request.POST.get("current_password", "")):
                        raise ValidationError({"current_password": _("Enter your current password to add a "
                                                                     "payout destination.")})
                    reauth.mark(request, request.user)
                services.add_beneficiary(request.user, kind=request.POST.get("kind", ""),
                                         holder_name=request.POST.get("holder_name", ""),
                                         identifier=request.POST.get("identifier", ""),
                                         network=request.POST.get("network", ""),
                                         bank_code=request.POST.get("bank_code", ""))
                messages.success(request, _("Payout destination saved. Only the last four digits are shown."))
            elif action == "remove":
                services.deactivate_beneficiary(request.user, request.POST.get("beneficiary"))
                messages.success(request, _("Payout destination removed."))
            elif action == "demo_verify":
                services.demo_verify_beneficiary(request.user, request.POST.get("beneficiary"))
                messages.success(request, _("Demo check passed. This is not a real account verification."))
            else:
                raise Http404
        except services.NotFound:
            raise Http404 from None
        except services.NotAuthorized:
            if not demo_unconfirmed(request.user):
                raise PermissionDenied from None
            messages.error(request, DEMO_UNCONFIRMED_BLOCKED)
        except PaymentsDisabled:
            messages.error(request, _("Demo checks are not available here."))
        except (ValidationError, services.WithdrawalError) as error:
            messages.error(request, _errors(error))
        return redirect("withdrawals:destinations")


class DemoDetailView(VerifiedAccountRequiredMixin, View):
    def get(self, request, withdrawal_id):
        demo = get_object_or_404(DemoWithdrawal, pk=withdrawal_id, member=request.user)
        decisions = list(demo.decisions.all())
        labels = dict(DemoWithdrawal.Status.choices)  # Decisions store the status code without its label.
        for decision in decisions:
            decision.status_label = labels.get(decision.to_status, "")
        return render(request, "withdrawals/demo.html", {"nav": "withdrawals", "demo": demo, "decisions": decisions})


@method_decorator(csrf_exempt, name="dispatch")
class SandboxPayoutCallbackView(View):
    """Provider notice: authenticated by HMAC signature, not browser CSRF."""

    def post(self, request):
        try:
            status, outcome = services.receive_payout_callback(request.body,
                                                               request.headers.get("X-Sandbox-Signature", ""))
        except PaymentsDisabled:
            raise Http404 from None
        return JsonResponse({"outcome": str(outcome)}, status=status)
