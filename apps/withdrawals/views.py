"""
Member withdrawal pages (/withdrawals/). Real withdrawals are unavailable; pages
explain why, manage payout destinations (masked), show a read-only preview, and
list DEMO scenarios. Every lookup is scoped to the signed-in member.
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.accounts.access import DEMO_UNCONFIRMED_BLOCKED, VerifiedAccountRequiredMixin, demo_unconfirmed
from apps.payments.providers import PaymentsDisabled, sandbox_active
from apps.wallets import wallet_services

from . import services
from .models import DemoWithdrawal, PayoutBeneficiary


def _errors(error):
    return " ".join(getattr(error, "messages", [str(error)]))


class OverviewView(VerifiedAccountRequiredMixin, View):
    def get(self, request):
        preview = None
        raw = request.GET.get("preview_amount", "").strip()
        if raw:
            try:
                preview = services.demo_preview(request.user, amount=int(raw) if raw.isdigit() else raw)
            except (ValidationError, ValueError) as error:
                messages.error(request, _errors(error))
        eligibility = services.withdrawal_eligibility(request.user)
        wallet = wallet_services.wallet_for(request.user)
        return render(request, "withdrawals/overview.html", {
            "nav": "withdrawals", "eligibility": eligibility,
            "messages_for": [services.ELIGIBILITY_MESSAGES.get(c, c) for c in eligibility.reasons],
            "wallet": wallet, "balances": wallet_services.balances(wallet) if wallet else None,
            "beneficiaries": PayoutBeneficiary.objects.filter(member=request.user, status="ACTIVE"),
            "demos": services.demo_for_member(request.user)[:20], "preview": preview,
            "sandbox": sandbox_active()})


class DestinationsView(VerifiedAccountRequiredMixin, View):
    def get(self, request):
        return render(request, "withdrawals/destinations.html", {
            "nav": "withdrawals", "beneficiaries": PayoutBeneficiary.objects.filter(member=request.user)
                                                                        .order_by("status", "-created_at"),
            "networks": PayoutBeneficiary.Network.choices, "sandbox": sandbox_active()})

    def post(self, request):
        action = request.POST.get("action", "")
        try:
            if action == "add":
                services.add_beneficiary(request.user, kind=request.POST.get("kind", ""),
                                         holder_name=request.POST.get("holder_name", ""),
                                         identifier=request.POST.get("identifier", ""),
                                         network=request.POST.get("network", ""),
                                         bank_code=request.POST.get("bank_code", ""))
                messages.success(request, _("Payout destination saved (masked)."))
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
