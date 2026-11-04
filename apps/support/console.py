"""
Staff support workspace in the administrator console (/manage/support/).
Reading needs support.view_tickets (or manage_tickets); every action needs
support.manage_tickets and is re-checked by the service layer.
"""

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views import View

from apps.administration.views import console, console_links

from . import services
from .forms import StaffReplyForm
from .models import Ticket

PAGE_SIZE = 25
CRUMBS = [("Overview", "administration:overview"), ("Support", "administration:support")]


def _status_choices():
    return [(value, _("Waiting for customer") if value == Ticket.Status.WAITING else label)
            for value, label in Ticket.Status.choices]


@console
class QueueView(View):
    def get(self, request):
        if not services.can_view_queue(request.user):
            raise PermissionDenied
        filters = {key: request.GET.get(key, "").strip()[:64]
                   for key in ("status", "category", "priority", "assignee", "q")}
        if "status" not in request.GET:
            filters["status"] = "active"
        tickets = services.staff_queue(status=filters["status"], category=filters["category"],
                                       priority=filters["priority"], assignee=filters["assignee"],
                                       query=filters["q"], user=request.user)
        page = Paginator(tickets, PAGE_SIZE).get_page(request.GET.get("page"))
        params = request.GET.copy()
        params.pop("page", None)
        return render(request, "administration/support/queue.html", {
            "page": page, "filters": filters, "statuses": _status_choices(),
            "categories": Ticket.Category.choices, "priorities": Ticket.Priority.choices,
            "agents": services.agents(), "page_query": params.urlencode(),
            "can": console_links(request.user), "nav": "support",
            "breadcrumbs": [("Overview", "administration:overview")]})


@console
class TicketView(View):
    def _ticket(self, request, reference):
        try:
            return services.ticket_for_staff(request.user, reference)
        except services.NotAuthorized:
            raise PermissionDenied from None
        except services.NotFound:
            raise Http404 from None

    def _render(self, request, ticket, reply_form=None, status=200):
        manage = services.can_manage(request.user)
        return render(request, "administration/support/ticket.html", {
            "ticket": ticket, "items": services.conversation(ticket, include_internal=True),
            "reply_form": reply_form or StaffReplyForm(), "can_manage": manage,
            "agents": services.agents() if manage else [],
            "next_statuses": [(value, label) for value, label in _status_choices()
                              if value in services.STAFF_TRANSITIONS[ticket.status]],
            "priorities": Ticket.Priority.choices, "can": console_links(request.user), "nav": "support",
            "breadcrumbs": CRUMBS}, status=status)

    def get(self, request, reference):
        return self._render(request, self._ticket(request, reference))

    def post(self, request, reference):
        ticket = self._ticket(request, reference)
        if not services.can_manage(request.user):
            raise PermissionDenied
        action = request.POST.get("action", "")
        form = None
        try:
            if action == "reply":
                form = StaffReplyForm(request.POST)
                if not form.is_valid():
                    return self._render(request, ticket, form, status=400)
                data = form.cleaned_data
                services.staff_reply(request.user, ticket, body=data["body"], internal=data["internal"],
                                     submission_key=data["submission_key"], then_status=data["then_status"])
                messages.success(request, _("Internal note added.") if data["internal"] else _("Reply sent."))
            elif action == "assign":
                assignee = None
                if request.POST.get("assignee"):
                    assignee = get_user_model().objects.filter(pk=request.POST["assignee"]).first() \
                        if services._is_uuid(request.POST["assignee"]) else None
                    if assignee is None:
                        raise ValidationError(_("Choose a staff member who handles support tickets."))
                services.assign(request.user, ticket, assignee, expected_status=request.POST.get("expected_status", ""))
                messages.success(request, _("Assignment updated."))
            elif action == "status":
                services.set_status(request.user, ticket, request.POST.get("status", ""),
                                    expected_status=request.POST.get("expected_status", ""))
                messages.success(request, _("Status updated."))
            elif action == "priority":
                services.set_priority(request.user, ticket, request.POST.get("priority", ""))
                messages.success(request, _("Priority updated."))
            else:
                messages.error(request, _("Unknown action."))
        except services.NotAuthorized:
            raise PermissionDenied from None
        except (ValidationError, services.SupportError) as error:
            for text in getattr(error, "messages", [str(error)]):
                messages.error(request, text)
            ticket.refresh_from_db()
            return self._render(request, ticket, form, status=400)
        return redirect("administration:support_ticket", ticket.reference)
