"""Member wallet pages (/wallet/). Only the signed-in member's own wallet."""

from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.accounts.access import DEMO_UNCONFIRMED_BLOCKED, VerifiedAccountRequiredMixin, demo_unconfirmed

from . import wallet_services
from .models import WalletTransaction


class WalletView(VerifiedAccountRequiredMixin, View):
    def get(self, request):
        reason = wallet_services.wallet_eligibility(request.user)
        wallet = wallet_services.wallet_for(request.user)
        return render(request, "wallets/wallet.html", {
            "nav": "wallet", "eligibility": reason, "wallet": wallet,
            "balances": wallet_services.balances(wallet) if wallet else None,
            "transactions": wallet_services.history(wallet)[:50] if wallet else [],
            "restrictions": wallet.restrictions.exclude(status="LIFTED") if wallet else []})

    def post(self, request):
        try:
            wallet_services.ensure_wallet(request.user)
            messages.success(request, _("Your wallet is open. It holds no money yet."))
        except wallet_services.NotEligible:
            messages.error(request, DEMO_UNCONFIRMED_BLOCKED if demo_unconfirmed(request.user)
                           else _("This account cannot have a member wallet."))
        return redirect("wallets:wallet")


class WalletTransactionView(VerifiedAccountRequiredMixin, View):
    def get(self, request, transaction_id):
        wallet = wallet_services.wallet_for(request.user)
        if wallet is None:
            raise Http404
        txn = get_object_or_404(WalletTransaction, pk=transaction_id, wallet=wallet)
        return render(request, "wallets/transaction.html", {"nav": "wallet", "wallet": wallet, "txn": txn})
