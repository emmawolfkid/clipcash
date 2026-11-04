"""
Customer support pages for members and advertisers (/support/). Every lookup goes
through services.visible_to_customer: another customer's ticket is "not found".
Changes are POST only (CSRF-protected) and validated by the service layer.
"""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views import View

from apps.administration.views import _form_errors

from . import services
from .forms import ReplyForm, TicketForm
from .models import Ticket

PAGE_SIZE = 20


class CustomerMixin(LoginRequiredMixin):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not services.is_customer(request.user):
            if services.can_view_queue(request.user):
                return redirect("administration:support")
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


def _ticket(user, reference):
    try:
        return services.ticket_for_customer(user, reference)
    except services.NotFound:
        raise Http404 from None


class TicketListView(CustomerMixin, View):
    def get(self, request):
        status = request.GET.get("status", "")
        query = request.GET.get("q", "").strip()[:100]
        page = Paginator(services.customer_tickets(request.user, status=status, query=query),
                         PAGE_SIZE).get_page(request.GET.get("page"))
        params = request.GET.copy()
        params.pop("page", None)
        return render(request, "support/ticket_list.html", {
            "page": page, "status": status, "query": query, "statuses": Ticket.Status.choices,
            "page_query": params.urlencode(), "nav": "support"})


class TicketCreateView(CustomerMixin, View):
    def _form(self, request, data=None):
        return TicketForm(data, organizations=services.customer_organizations(request.user).order_by("display_name"))

    def get(self, request):
        return render(request, "support/ticket_form.html", {"form": self._form(request), "nav": "support"})

    def post(self, request):
        form = self._form(request, request.POST)
        if form.is_valid():
            data = form.cleaned_data
            try:
                ticket = services.create_ticket(
                    request.user, subject=data["subject"], category=data["category"],
                    description=data["description"], submission_key=data["submission_key"],
                    organization=data.get("organization"))
            except (ValidationError, services.SupportError) as error:
                _form_errors(form, error)
            else:
                messages.success(request, _("Your request %(reference)s was received. We will reply here.")
                                 % {"reference": ticket.reference})
                return redirect("support:ticket", ticket.reference)
        return render(request, "support/ticket_form.html", {"form": form, "nav": "support"}, status=400)


class TicketDetailView(CustomerMixin, View):
    def _render(self, request, ticket, form, status=200):
        return render(request, "support/ticket_detail.html", {
            "ticket": ticket, "items": services.conversation(ticket, include_internal=False),
            "form": form, "can_reply": not ticket.is_closed, "nav": "support"}, status=status)

    def get(self, request, reference):
        ticket = _ticket(request.user, reference)
        return self._render(request, ticket, ReplyForm())

    def post(self, request, reference):
        ticket = _ticket(request.user, reference)
        form = ReplyForm(request.POST)
        if form.is_valid():
            try:
                services.customer_reply(request.user, ticket, body=form.cleaned_data["body"],
                                        submission_key=form.cleaned_data["submission_key"])
            except services.NotFound:
                raise Http404 from None
            except (ValidationError, services.SupportError) as error:
                _form_errors(form, error)
            else:
                messages.success(request, _("Your reply was sent."))
                return redirect("support:ticket", ticket.reference)
        ticket.refresh_from_db()
        return self._render(request, ticket, form, status=400)
