"""PAY-LIVE-01D: Operations Console membership management for one member.

Read: the member's current membership, live periods, history and membership audit trail.
Change: suspend, reinstate and cancel (memberships.manage_memberships or Super Administrator)
and extend (Super Administrator only), each on its own confirmation page, POST and CSRF
only. Every rule is enforced by apps.memberships.services; these views add none of their own
beyond refusing to show a form the administrator could never submit.
"""

import uuid

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.audit.models import AuditEvent
from apps.memberships import admin_forms
from apps.memberships import services as memberships
from apps.memberships.models import Subscription

from .views import _form_errors, console, console_links

S = Subscription.Status
HISTORY_LIMIT = 20
AUDIT_LIMIT = 25

ACTIONS = {
    "suspend": {"form": admin_forms.SuspendForm, "title": _("Suspend membership"),
                "button": _("Suspend membership"),
                "explain": _("Access to membership content stops immediately. The period keeps running; "
                             "reinstating later does not add the lost time back.")},
    "reinstate": {"form": admin_forms.ReinstateForm, "title": _("Reinstate membership"),
                  "button": _("Reinstate membership"),
                  "explain": _("Access is restored until the period's original end. A period that has already "
                               "ended becomes expired instead, and an ineligible account is refused.")},
    "cancel": {"form": admin_forms.CancelForm, "title": _("Cancel membership"),
               "button": _("Cancel membership"),
               "explain": _("Access ends immediately and cannot be restored. No refund is made and no payment or "
                            "ledger entry is reversed; refunds follow the separate manual procedure.")},
    "extend": {"form": admin_forms.ExtensionForm, "title": _("Extend membership"),
               "button": _("Extend membership"),
               "explain": _("Adds a separate period after the member's current access ends, on the same plan. It is "
                            "an administrative adjustment: no payment is recorded and nothing is charged.")},
}

SUCCESS = {
    "suspend": _("Membership suspended."),
    "reinstate": _("Membership reinstated."),
    "cancel": _("Membership cancelled. No refund or ledger reversal was made."),
}


def _can_manage(user):
    return memberships.can_manage_subscriptions(user)


def available_actions(subscription, user, now):
    """The actions this administrator may take on this period in its current state."""
    if subscription.member_id == user.pk or subscription.expires_at is None or subscription.expires_at <= now:
        return []
    actions = []
    if _can_manage(user):
        if subscription.status == S.ACTIVE:
            actions.append("suspend")
        if subscription.status == S.SUSPENDED:
            actions.append("reinstate")
        if subscription.status in (S.ACTIVE, S.SUSPENDED):
            actions.append("cancel")
    if memberships.can_extend_memberships(user) and subscription.is_in_force(now):
        actions.append("extend")
    return actions


def member_detail_context(user, member, can):
    """Extra context for the console member page (administration.views.MemberDetailView)."""
    now = timezone.now()
    history = list(member.subscriptions.order_by("-created_at")[:HISTORY_LIMIT])
    for subscription in history:
        subscription.console_actions = available_actions(subscription, user, now)
    live = [s for s in history if s.status in (S.ACTIVE, S.SUSPENDED) and s.expires_at and s.expires_at > now]
    current = next((s for s in live if s.starts_at <= now), None)
    show_audit = bool(can.get("audit") or _can_manage(user))
    audit = []
    if show_audit and history:
        audit = list(AuditEvent.objects.filter(target_model="subscription",
                                               target_id__in=[str(s.pk) for s in history])
                     .order_by("-occurred_at")[:AUDIT_LIMIT])
    return {"current_period": current, "scheduled_periods": [s for s in live if s.starts_at > now],
            "membership_end": max((s.expires_at for s in live), default=None),
            "membership_history": history, "membership_audit": audit, "show_membership_audit": show_audit,
            "can_manage_memberships": _can_manage(user), "now": now}


@console
class MembershipActionView(View):
    template_name = "administration/members/membership_action.html"

    def _load(self, request, pk, subscription_id, action):
        if action not in ACTIONS:
            raise PermissionDenied
        allowed = (memberships.can_extend_memberships(request.user) if action == "extend"
                   else _can_manage(request.user))
        if not allowed:  # Server side, whatever the page showed.
            raise PermissionDenied
        User = get_user_model()
        member = get_object_or_404(User, pk=pk, role=User.Role.MEMBER)
        subscription = get_object_or_404(Subscription, pk=subscription_id, member=member)
        return member, subscription

    def _render(self, request, member, subscription, action, form, status=200):
        now = timezone.now()
        later = [p for p in memberships.live_periods(member, now) if p.starts_at > now and p.pk != subscription.pk]
        follows = [p for p in later if p.activation_source == Subscription.Source.EXTENSION
                   and subscription.expires_at and p.starts_at >= subscription.expires_at]
        return render(request, self.template_name, {
            "member": member, "subscription": subscription, "action": action, "form": form,
            "spec": ACTIONS[action], "membership_end": memberships.membership_end(member, now),
            # PAY-LIVE-01D: later administrative extensions follow a suspension, reinstatement or
            # cancellation of this period; other later periods (paid renewals) are not changed.
            "following_extensions": follows,
            "scheduled_periods": [p for p in later if p not in follows],
            "can": console_links(request.user), "nav": "members",
            "breadcrumbs": [("Overview", "administration:overview"), ("Members", "administration:members")],
        }, status=status)

    def get(self, request, pk, subscription_id, action):
        member, subscription = self._load(request, pk, subscription_id, action)
        if action not in available_actions(subscription, request.user, timezone.now()):
            messages.error(request, _("That action is not available for this membership in its current state."))
            return redirect("administration:member", pk=member.pk)
        initial = {}
        if action == "extend":
            end = memberships.membership_end(member)
            initial = {"operation": uuid.uuid4().hex, "expected_end": end.isoformat() if end else ""}
        return self._render(request, member, subscription, action, ACTIONS[action]["form"](initial=initial))

    def post(self, request, pk, subscription_id, action):
        member, subscription = self._load(request, pk, subscription_id, action)
        form = ACTIONS[action]["form"](request.POST)
        if not form.is_valid():
            return self._render(request, member, subscription, action, form, status=400)
        data = form.cleaned_data
        try:
            if action == "extend":
                extension, created = memberships.extend(
                    request.user, subscription, days=data["days"], reason=data["reason"],
                    operation_id=data["operation"], expected_end=data["expected_end"])
                end = timezone.localtime(extension.expires_at).strftime("%Y-%m-%d %H:%M %Z")
                messages.success(request, (_("Membership extended until %(end)s. No payment was recorded.") if created
                                           else _("This extension was already made (until %(end)s); nothing was "
                                                  "added.")) % {"end": end})
            elif action == "suspend":
                memberships.suspend(request.user, subscription, data["reason"], note=data["note"])
            elif action == "cancel":
                memberships.cancel(request.user, subscription, data["reason"], note=data["note"])
            else:
                result = memberships.reinstate(request.user, subscription, note=data["note"])
                if result.status == S.EXPIRED:
                    messages.warning(request, _("This period had already ended, so it was marked expired. Access "
                                                "was not restored."))
                    return redirect("administration:member", pk=member.pk)
        except memberships.NotAuthorized:
            raise PermissionDenied from None
        except (ValidationError, memberships.MembershipError) as error:
            _form_errors(form, error)
            return self._render(request, member, subscription, action, form, status=400)
        if action in SUCCESS:
            messages.success(request, SUCCESS[action])
        return redirect("administration:member", pk=member.pk)
