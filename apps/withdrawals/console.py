"""FIN-02 administration console: real withdrawals, payout processing, settings and identity checks.
Every page enforces its own permission; every action goes through apps.withdrawals.real."""

import datetime

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.cache import never_cache

from apps.accounts.lookup import identifier_q
from apps.administration.views import _error_text, console, console_links
from apps.audit.models import AuditEvent
from apps.wallets import wallet_services
from apps.wallets.models import WalletTransaction

from . import protect, real
from .models import WithdrawalIdentity, WithdrawalRequest, WithdrawalSettings

W = WithdrawalRequest.Status


def _ctx(request, nav, **extra):
    return {"can": console_links(request.user), "nav": nav, "requests_active": real.requests_active(),
            "settlement_active": real.settlement_active(), **extra}


@console
class WithdrawalRequestsView(View):
    def get(self, request):
        if not real.can_view(request.user):
            raise PermissionDenied
        qs = WithdrawalRequest.objects.select_related("member").order_by("-created_at")
        filters = {key: request.GET.get(key, "").strip()[:100] for key in ("q", "status", "method", "start", "end")}
        if filters["q"]:
            qs = qs.filter(Q(reference__iexact=filters["q"]) | identifier_q(filters["q"], "member__"))
        if filters["status"] in W.values:
            qs = qs.filter(status=filters["status"])
        if filters["method"] in WithdrawalRequest.Method.values:
            qs = qs.filter(method=filters["method"])
        for key, lookup in (("start", "created_at__date__gte"), ("end", "created_at__date__lte")):
            try:
                if filters[key]:
                    qs = qs.filter(**{lookup: datetime.date.fromisoformat(filters[key])})
            except ValueError:
                filters[key] = ""
        page = Paginator(qs, 25).get_page(request.GET.get("page"))
        params = request.GET.copy()
        params.pop("page", None)
        return render(request, "administration/withdrawals_real/list.html", _ctx(
            request, "withdrawal_requests", page=page, filters=filters, page_query=params.urlencode(),
            statuses=W.choices, methods=WithdrawalRequest.Method.choices, summary=real.summary(),
            identities_waiting=WithdrawalIdentity.objects.filter(status="SUBMITTED").count()))


@console
@method_decorator(never_cache, name="dispatch")  # The page can show a revealed payout number.
class WithdrawalRequestView(View):
    template = "administration/withdrawals_real/detail.html"

    def _context(self, request, withdrawal, **extra):
        wallet = withdrawal.wallet
        identity = real.identity_of(withdrawal.member)
        user = request.user
        return _ctx(
            request, "withdrawal_requests", withdrawal=withdrawal, identity=identity,
            balances=wallet_services.balances(wallet), restrictions=wallet.restrictions.exclude(status="LIFTED"),
            events=withdrawal.events.select_related("actor"),
            ledger=WalletTransaction.objects.filter(journal_entry__in=[e for e in (
                withdrawal.reserve_entry_id, withdrawal.release_entry_id, withdrawal.payout_entry_id) if e])
            .order_by("created_at"),
            audit=AuditEvent.objects.filter(target_id=str(withdrawal.pk)).order_by("-occurred_at")[:30],
            reasons=real.REJECTION_REASONS, reason_messages=real.REASON_MESSAGES,
            can_approve=real.can_approve(user) and user.pk != withdrawal.member_id,
            can_process=real.can_process(user) and user.pk not in (withdrawal.approved_by_id, withdrawal.member_id),
            can_verify=real.can_verify_payouts(user) and user.pk not in (withdrawal.claimed_by_id, withdrawal.member_id),
            is_operator=withdrawal.claimed_by_id == user.pk, identity_required=real.identity_required(
                withdrawal.amount, withdrawal.settings_version), **extra)

    def get(self, request, pk):
        if not real.can_view(request.user):
            raise PermissionDenied
        withdrawal = get_object_or_404(WithdrawalRequest.objects.select_related(
            "member", "wallet", "settings_version", "approved_by", "claimed_by", "verified_by"), pk=pk)
        return render(request, self.template, self._context(request, withdrawal))

    def post(self, request, pk):
        withdrawal = get_object_or_404(WithdrawalRequest, pk=pk)
        action = request.POST.get("action", "")
        expected = request.POST.get("expected_status", "")
        user = request.user
        try:
            if action == "review":
                real.start_review(user, withdrawal, expected_status=expected)
            elif action == "info":
                real.request_information(user, withdrawal, note=request.POST.get("note", ""))
            elif action == "approve":
                real.approve(user, withdrawal, expected_status=expected, note=request.POST.get("note", ""))
            elif action == "reject":
                real.reject(user, withdrawal, expected_status=expected, reason_code=request.POST.get("reason_code", ""),
                            note=request.POST.get("note", ""))
            elif action == "claim":
                real.claim(user, withdrawal, expected_status=expected or W.APPROVED)
            elif action == "reveal":
                number = real.reveal_destination(user, withdrawal)
                withdrawal.refresh_from_db()
                return render(request, self.template, self._context(request, withdrawal, revealed=number))
            elif action == "record":
                real.record_payment(user, withdrawal, payout_reference=request.POST.get("payout_reference", ""),
                                    evidence=request.POST.get("evidence", ""))
            elif action == "problem":
                real.report_problem(user, withdrawal, note=request.POST.get("note", ""))
            elif action == "verify":
                real.verify_payment(user, withdrawal, confirmed=request.POST.get("confirmed") == "on",
                                    note=request.POST.get("note", ""))
            elif action == "failed":
                real.confirm_failed(user, withdrawal, note=request.POST.get("note", ""))
            else:
                raise PermissionDenied
            messages.success(request, _("Withdrawal updated."))
        except real.NotAuthorized as error:
            if error.code == "permission_missing":
                raise PermissionDenied from None
            messages.error(request, _error_text(error))
        except protect.ProtectionUnavailable:
            messages.error(request, _("The payout number cannot be read: the payout data key is not configured."))
        except (ValidationError, real.WithdrawalFlowError) as error:
            messages.error(request, _error_text(error))
        return redirect("administration:withdrawal_request", pk=pk)


@console
class WithdrawalSettingsView(View):
    def get(self, request):
        if not (real.can_manage_settings(request.user) or real.can_view(request.user)):
            raise PermissionDenied
        return render(request, "administration/withdrawals_real/settings.html", _ctx(
            request, "withdrawal_settings", current=real.current_settings(),
            history=WithdrawalSettings.objects.select_related("created_by").order_by("-version")[:15],
            methods=WithdrawalRequest.Method.choices, fee_modes=WithdrawalSettings.FeeMode.choices,
            fee_bearers=WithdrawalSettings.FeeBearer.choices, can_manage=real.can_manage_settings(request.user)))

    def post(self, request):
        try:
            values = {name: request.POST.get(name) for name in real.SETTING_FIELDS}
            values["methods"] = request.POST.getlist("methods")
            values["requests_open"] = request.POST.get("requests_open") == "on"
            values["payouts_open"] = request.POST.get("payouts_open") == "on"
            real.update_settings(request.user, expected_version=request.POST.get("expected_version", ""),
                                 reason=request.POST.get("reason", ""), **values)
            messages.success(request, _("Withdrawal settings saved as a new version. Existing requests keep theirs."))
        except real.NotAuthorized:
            raise PermissionDenied from None
        except ValidationError as error:
            messages.error(request, _error_text(error))
        return redirect("administration:withdrawal_settings")


@console
class WithdrawalIdentitiesView(View):
    def get(self, request):
        if not real.can_verify_identity(request.user):
            raise PermissionDenied
        status = request.GET.get("status", "SUBMITTED")
        qs = WithdrawalIdentity.objects.select_related("member", "reviewed_by").order_by("-updated_at")
        if status in WithdrawalIdentity.Status.values:
            qs = qs.filter(status=status)
        return render(request, "administration/withdrawals_real/identities.html", _ctx(
            request, "withdrawal_identities", page=Paginator(qs, 25).get_page(request.GET.get("page")),
            status=status, statuses=WithdrawalIdentity.Status.choices))

    def post(self, request):
        identity = get_object_or_404(WithdrawalIdentity, pk=request.POST.get("identity"))
        try:
            real.review_identity(request.user, identity, approve=request.POST.get("action") == "verify",
                                 note=request.POST.get("note", ""))
            messages.success(request, _("Identity check updated."))
        except real.NotAuthorized as error:
            if error.code == "permission_missing":
                raise PermissionDenied from None
            messages.error(request, _error_text(error))
        except (ValidationError, real.WithdrawalFlowError) as error:
            messages.error(request, _error_text(error))
        return redirect("administration:withdrawal_identities")
