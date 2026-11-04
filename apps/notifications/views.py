"""
Notification inbox (Phase 15-A). Every lookup is limited to the signed-in account:
another account's notice is "not found". State changes are POST only (CSRF-protected).
Links are internal paths, re-checked before redirecting.
"""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from . import inbox
from .models import Notification

PAGE_SIZE = 20


def _back_to_inbox(request):
    """Return to the inbox page the user came from (same-site path only)."""
    target = request.POST.get("next", "")
    if target.startswith("/notifications/") and url_has_allowed_host_and_scheme(target, allowed_hosts=None):
        return redirect(target)
    return redirect("notifications:inbox")


class InboxView(LoginRequiredMixin, View):
    def get(self, request):
        category = request.GET.get("category", "")
        unread = request.GET.get("unread") == "1"
        notices = inbox.for_recipient(request.user, category=category, unread=unread)
        page = Paginator(notices, PAGE_SIZE).get_page(request.GET.get("page"))
        query = request.GET.copy()
        query.pop("page", None)
        return render(request, "notifications/inbox.html", {
            "page": page, "categories": Notification.Category.choices,
            "category": category if category in Notification.Category.values else "", "unread_only": unread,
            "page_query": query.urlencode(), "current_path": request.get_full_path(), "nav": "notifications",
        })


class OpenView(LoginRequiredMixin, View):
    """Mark read and go to the related page (or back to the inbox when there is none)."""

    def post(self, request, notification_id):
        notice = inbox.mark_read(request.user, notification_id)
        if notice is None:
            raise Http404
        link = inbox.safe_link(notice.link)
        return redirect(link) if link else redirect("notifications:inbox")


class MarkReadView(LoginRequiredMixin, View):
    def post(self, request, notification_id):
        if inbox.mark_read(request.user, notification_id) is None:
            raise Http404
        return _back_to_inbox(request)


class MarkAllReadView(LoginRequiredMixin, View):
    def post(self, request):
        inbox.mark_all_read(request.user)
        return _back_to_inbox(request)
