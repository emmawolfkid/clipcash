"""
Customer support services (Phase 15-B): the only supported way to change tickets.

Customers (ACTIVE member accounts) create tickets and reply. Staff with explicit
permissions (support.view_tickets / support.manage_tickets; superusers have both)
see the queue, assign, answer, add internal notes and change status. Every change
locks the ticket row, is validated here and again by PostgreSQL (migration 0002),
is audited, and notifies through apps.notifications.inbox in the same transaction.

Visibility of organization tickets (decision D15, default): the requester while still
an active team member, and the organization's active OWNERS. Managers see only their
own tickets. Internal notes are never shown or notified to customers.
"""

import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.throttling import Throttle
from apps.advertising.models import AdvertiserMembership
from apps.audit.services import DENIED, record_business_event, record_security_event

from .models import MAX_BODY, MAX_SUBJECT, Ticket, TicketEvent, TicketMessage

S = Ticket.Status
OPEN_STATES = (S.OPEN, S.IN_PROGRESS, S.WAITING)
SUBMISSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
REFERENCE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # No 0/O, 1/I/L.
TICKETS_PER_HOUR = Throttle("support-ticket", limit=5, window_seconds=60 * 60)
REPLIES_PER_HOUR = Throttle("support-reply", limit=30, window_seconds=60 * 60)
# Staff may move a ticket between these (CLOSED is final; RESOLVED may be reopened).
STAFF_TRANSITIONS = {
    S.OPEN: {S.IN_PROGRESS, S.WAITING, S.RESOLVED, S.CLOSED},
    S.IN_PROGRESS: {S.OPEN, S.WAITING, S.RESOLVED, S.CLOSED},
    S.WAITING: {S.OPEN, S.IN_PROGRESS, S.RESOLVED, S.CLOSED},
    S.RESOLVED: {S.IN_PROGRESS, S.CLOSED},
    S.CLOSED: set(),
}


class SupportError(Exception):
    code = "support_error"

    def __init__(self, message, *, code=None):
        super().__init__(message)
        if code:
            self.code = code


class NotAuthorized(SupportError):
    code = "not_authorized"


class NotFound(SupportError):
    code = "not_found"


class InvalidState(SupportError):
    code = "invalid_state"


class RateLimited(SupportError):
    code = "rate_limited"


# --- Who may do what -----------------------------------------------------------------------


def _staff_with(user, perm):
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.is_active
                and user.is_staff and user.has_perm(perm))


def can_view_queue(user):
    return _staff_with(user, "support.view_tickets") or _staff_with(user, "support.manage_tickets")


def can_manage(user):
    return _staff_with(user, "support.manage_tickets")


def is_customer(user):
    User = get_user_model()
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.is_active
                and user.status == User.Status.ACTIVE and user.role == User.Role.MEMBER)


def agents():
    """Staff who may be assigned tickets (active, with support.manage_tickets)."""
    User = get_user_model()
    condition = (Q(is_superuser=True)
                 | Q(user_permissions__content_type__app_label="support", user_permissions__codename="manage_tickets")
                 | Q(groups__permissions__content_type__app_label="support", groups__permissions__codename="manage_tickets"))
    return User.objects.filter(condition, is_active=True, is_staff=True).distinct().order_by("email")


def _deny(action, actor, target=None, reason="permission_missing"):
    record_security_event("support.action.denied", outcome=DENIED,
                          actor=actor if getattr(actor, "is_authenticated", False) else None, target=target,
                          metadata={"action": action, "reason": reason})
    raise NotAuthorized("You are not allowed to do this.", code=reason)


def customer_organizations(user):
    """Organizations the customer may file tickets for (active team membership)."""
    from apps.advertising.models import Organization

    return Organization.objects.filter(memberships__user=user, memberships__is_active=True).distinct()


def visible_to_customer(user):
    """Tickets this customer may see (see module docstring)."""
    if not is_customer(user):
        return Ticket.objects.none()
    active_orgs = AdvertiserMembership.objects.filter(user=user, is_active=True)
    owned = active_orgs.filter(role=AdvertiserMembership.Role.OWNER).values("organization_id")
    member_of = active_orgs.values("organization_id")
    return Ticket.objects.filter(
        Q(requester=user, organization__isnull=True)
        | Q(requester=user, organization__in=member_of)
        | Q(organization__in=owned))


def ticket_for_customer(user, reference):
    ticket = visible_to_customer(user).filter(reference=str(reference).upper()).first()
    if ticket is None:
        raise NotFound("Ticket not found.")
    return ticket


def ticket_for_staff(user, reference):
    if not can_view_queue(user):
        _deny("ticket.view", user)
    ticket = Ticket.objects.filter(reference=str(reference).upper()).first()
    if ticket is None:
        raise NotFound("Ticket not found.")
    return ticket


# --- Helpers ---------------------------------------------------------------------------------


def _text(value, field, *, minimum, maximum):
    value = str(value or "").strip()
    if len(value) < minimum:
        raise ValidationError({field: f"Please write at least {minimum} characters."})
    if len(value) > maximum:
        raise ValidationError({field: f"Please keep this to {maximum} characters or fewer."})
    return value


def _submission(key):
    key = str(key or "")
    if not SUBMISSION_RE.match(key):
        raise ValidationError("This form has expired. Reload the page and try again.")
    return key


def _new_reference():
    return "CS-" + "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(8))


def _event(ticket, actor, action, from_value="", to_value=""):
    TicketEvent.objects.create(ticket=ticket, actor=actor, action=action, from_value=from_value, to_value=to_value)


def _locked(ticket):
    return Ticket.objects.select_for_update(of=("self",)).select_related("requester", "organization", "assignee").get(pk=ticket.pk)


def _set_status(ticket, actor, new_status, now):
    old = ticket.status
    ticket.status = new_status
    if new_status == S.RESOLVED:
        ticket.resolved_at = now
    if new_status == S.CLOSED:
        ticket.closed_at = now
    _event(ticket, actor, TicketEvent.Action.STATUS, old, new_status)
    return old


# --- Customer actions ------------------------------------------------------------------------


def create_ticket(user, *, subject, category, description, submission_key, organization=None):
    """A customer opens a ticket. Repeating the same form returns the original ticket."""
    if not is_customer(user):
        _deny("ticket.create", user, reason="not_a_customer")
    if category not in Ticket.Category.values:
        raise ValidationError({"category": "Choose a category."})
    subject = _text(subject, "subject", minimum=5, maximum=MAX_SUBJECT)
    description = _text(description, "description", minimum=10, maximum=MAX_BODY)
    submission_key = _submission(submission_key)
    if organization is not None and not customer_organizations(user).filter(pk=organization.pk).exists():
        _deny("ticket.create", user, organization, "not_a_team_member")
    existing = Ticket.objects.filter(requester=user, submission_key=submission_key).first()
    if existing is not None:
        return existing
    if TICKETS_PER_HOUR.is_limited(user.pk):
        raise RateLimited("You have opened several tickets recently. Please wait a while, or reply to an "
                          "existing ticket.")
    now = timezone.now()
    try:
        with transaction.atomic():
            ticket = None
            for _ in range(5):  # A reference collision is astronomically rare; retry anyway.
                try:
                    with transaction.atomic():
                        ticket = Ticket.objects.create(
                            reference=_new_reference(), requester=user, organization=organization, subject=subject,
                            category=category, submission_key=submission_key, last_activity_at=now)
                    break
                except IntegrityError as error:
                    if "support_ticket_submitted_once" in str(error):
                        raise
            if ticket is None:
                raise InvalidState("Could not create the ticket. Please try again.")
            TicketMessage.objects.create(ticket=ticket, author=user, body=description, submission_key=submission_key)
            _event(ticket, user, TicketEvent.Action.CREATED, "", S.OPEN)
            record_business_event("support.ticket.created", actor=user, target=ticket, metadata={
                "reference": ticket.reference, "category": category,
                "organization": str(organization.pk) if organization else ""})
            _notify_created(ticket)
    except IntegrityError:
        existing = Ticket.objects.filter(requester=user, submission_key=submission_key).first()
        if existing is None:
            raise
        return existing  # A concurrent submission of the same form won.
    TICKETS_PER_HOUR.hit(user.pk)
    return ticket


def customer_reply(user, ticket, *, body, submission_key):
    """The customer answers. Waiting tickets go back to the team; a resolved ticket is
    reopened within SUPPORT_REOPEN_DAYS; closed tickets take no replies."""
    ticket = ticket_for_customer(user, ticket.reference)  # Re-check access on every write.
    body = _text(body, "body", minimum=2, maximum=MAX_BODY)
    submission_key = _submission(submission_key)
    if REPLIES_PER_HOUR.is_limited(user.pk):
        raise RateLimited("You have sent many replies recently. Please wait a while.")
    now = timezone.now()
    with transaction.atomic():
        ticket = _locked(ticket)
        duplicate = TicketMessage.objects.filter(ticket=ticket, author=user, submission_key=submission_key).first()
        if duplicate is not None:
            return duplicate
        if ticket.status == S.CLOSED:
            raise InvalidState("This ticket is closed. Please open a new ticket.", code="closed")
        reopened = False
        if ticket.status == S.RESOLVED:
            window = timedelta(days=settings.SUPPORT_REOPEN_DAYS)
            if ticket.resolved_at and now - ticket.resolved_at > window:
                raise InvalidState(f"This ticket was resolved more than {settings.SUPPORT_REOPEN_DAYS} days ago. "
                                   "Please open a new ticket.", code="reopen_window_passed")
            reopened = True
        message = TicketMessage.objects.create(ticket=ticket, author=user, body=body, submission_key=submission_key)
        if ticket.status in (S.WAITING, S.RESOLVED):
            _set_status(ticket, user, S.IN_PROGRESS if ticket.assignee_id else S.OPEN, now)
            if reopened:
                _event(ticket, user, TicketEvent.Action.REOPENED, S.RESOLVED, ticket.status)
                ticket.resolved_at = None
        ticket.last_activity_at = now
        ticket.save()
        record_business_event("support.ticket.customer_replied", actor=user, target=ticket,
                              metadata={"reference": ticket.reference, "reopened": reopened})
        _notify_customer_reply(ticket, message, reopened)
    REPLIES_PER_HOUR.hit(user.pk)
    return message


# --- Staff actions ---------------------------------------------------------------------------


def _require_manage(actor, action, ticket=None):
    if not can_manage(actor):
        _deny(action, actor, ticket)


def _check_expected(ticket, expected_status):
    if expected_status and ticket.status != expected_status:
        raise InvalidState("This ticket changed since you opened it. Review it again.", code="stale")


def assign(actor, ticket, assignee, *, expected_status=""):
    """Assign (or with ``assignee=None`` unassign). Assignees must be support agents."""
    _require_manage(actor, "ticket.assign", ticket)
    if assignee is not None and not agents().filter(pk=assignee.pk).exists():
        raise ValidationError({"assignee": "Choose a staff member who handles support tickets."})
    now = timezone.now()
    with transaction.atomic():
        ticket = _locked(ticket)
        _check_expected(ticket, expected_status)
        if ticket.status == S.CLOSED:
            raise InvalidState("Closed tickets cannot be reassigned.", code="closed")
        if ticket.assignee_id == getattr(assignee, "pk", None):
            return ticket
        previous = ticket.assignee
        ticket.assignee = assignee
        if assignee is not None and ticket.status == S.OPEN:
            _set_status(ticket, actor, S.IN_PROGRESS, now)
        elif assignee is None and ticket.status == S.IN_PROGRESS:
            _set_status(ticket, actor, S.OPEN, now)
        ticket.last_activity_at = now
        ticket.save()
        _event(ticket, actor, TicketEvent.Action.ASSIGNED, getattr(previous, "email", "")[:40],
               getattr(assignee, "email", "")[:40])
        record_business_event("support.ticket.assigned", actor=actor, target=ticket, metadata={
            "reference": ticket.reference, "assignee": str(assignee.pk) if assignee else ""})
        _notify_assigned(ticket, actor)
    return ticket


def staff_reply(actor, ticket, *, body, internal, submission_key, then_status=""):
    """Staff answer the customer (``internal=False``) or add an internal note. A public
    reply may also move the ticket (e.g. to waiting-for-customer or resolved)."""
    _require_manage(actor, "ticket.reply", ticket)
    body = _text(body, "body", minimum=2, maximum=MAX_BODY)
    submission_key = _submission(submission_key)
    now = timezone.now()
    with transaction.atomic():
        ticket = _locked(ticket)
        duplicate = TicketMessage.objects.filter(ticket=ticket, author=actor, submission_key=submission_key).first()
        if duplicate is not None:
            return duplicate
        if ticket.status == S.CLOSED:
            raise InvalidState("This ticket is closed.", code="closed")
        if then_status and (internal or then_status not in STAFF_TRANSITIONS[ticket.status]):
            raise ValidationError({"then_status": "Choose a valid next status."})
        message = TicketMessage.objects.create(ticket=ticket, author=actor, body=body, internal=internal,
                                               from_staff=True, submission_key=submission_key)
        if then_status:
            _set_status(ticket, actor, then_status, now)
        if not internal:
            ticket.last_activity_at = now
        ticket.save()
        record_business_event("support.ticket.note_added" if internal else "support.ticket.staff_replied",
                              actor=actor, target=ticket,
                              metadata={"reference": ticket.reference, "status": ticket.status})
        if not internal:
            _notify_staff_reply(ticket, message)
            if then_status in (S.RESOLVED, S.CLOSED):
                _notify_outcome(ticket)
    return message


def set_status(actor, ticket, new_status, *, expected_status):
    _require_manage(actor, "ticket.status", ticket)
    now = timezone.now()
    with transaction.atomic():
        ticket = _locked(ticket)
        if ticket.status == new_status:
            return ticket
        _check_expected(ticket, expected_status)
        if new_status not in STAFF_TRANSITIONS.get(ticket.status, set()):
            raise InvalidState(f"A {ticket.get_status_display().lower()} ticket cannot become "
                               f"{Ticket.Status(new_status).label.lower() if new_status in S.values else new_status}.")
        old = _set_status(ticket, actor, new_status, now)
        if old == S.RESOLVED and new_status == S.IN_PROGRESS:
            ticket.resolved_at = None
            _event(ticket, actor, TicketEvent.Action.REOPENED, old, new_status)
        ticket.last_activity_at = now
        ticket.save()
        record_business_event("support.ticket.status_changed", actor=actor, target=ticket,
                              metadata={"reference": ticket.reference, "from_status": old, "to_status": new_status})
        if new_status in (S.RESOLVED, S.CLOSED):
            _notify_outcome(ticket)
    return ticket


def set_priority(actor, ticket, priority):
    _require_manage(actor, "ticket.priority", ticket)
    if priority not in Ticket.Priority.values:
        raise ValidationError({"priority": "Choose a priority."})
    with transaction.atomic():
        ticket = _locked(ticket)
        if ticket.status == S.CLOSED:
            raise InvalidState("Closed tickets cannot change.", code="closed")
        if ticket.priority == priority:
            return ticket
        old, ticket.priority = ticket.priority, priority
        ticket.save()
        _event(ticket, actor, TicketEvent.Action.PRIORITY, old, priority)
        record_business_event("support.ticket.priority_changed", actor=actor, target=ticket,
                              metadata={"reference": ticket.reference, "from_priority": old, "to_priority": priority})
    return ticket


# --- Queries ---------------------------------------------------------------------------------


def staff_queue(*, status="", category="", priority="", assignee="", query="", user=None):
    tickets = Ticket.objects.select_related("requester", "organization", "assignee")
    if status == "active":
        tickets = tickets.filter(status__in=OPEN_STATES)
    elif status in S.values:
        tickets = tickets.filter(status=status)
    if category in Ticket.Category.values:
        tickets = tickets.filter(category=category)
    if priority in Ticket.Priority.values:
        tickets = tickets.filter(priority=priority)
    if assignee == "none":
        tickets = tickets.filter(assignee__isnull=True)
    elif assignee == "me" and user is not None:
        tickets = tickets.filter(assignee=user)
    elif assignee:
        tickets = tickets.filter(assignee_id=assignee) if _is_uuid(assignee) else tickets.none()
    query = (query or "").strip()
    if query:
        tickets = tickets.filter(Q(reference__iexact=query) | Q(reference__icontains=query.upper())
                                 | Q(subject__icontains=query))
    return tickets.order_by("-last_activity_at")


def customer_tickets(user, *, status="", query=""):
    tickets = visible_to_customer(user).select_related("organization")
    if status == "active":
        tickets = tickets.filter(status__in=OPEN_STATES)
    elif status in S.values:
        tickets = tickets.filter(status=status)
    query = (query or "").strip()
    if query:
        tickets = tickets.filter(Q(reference__icontains=query.upper()) | Q(subject__icontains=query))
    return tickets.order_by("-last_activity_at")


def conversation(ticket, *, include_internal):
    """Messages and history in time order: [(created_at, kind, object)]. Customers see
    no internal notes, and no assignment or priority history (staff identities)."""
    messages = ticket.messages.select_related("author")
    events = ticket.events.select_related("actor")
    if not include_internal:
        messages = messages.filter(internal=False)
        events = events.exclude(action__in=[TicketEvent.Action.ASSIGNED, TicketEvent.Action.PRIORITY])
    items = [(m.created_at, "message", m) for m in messages]
    items += [(e.created_at, "event", e) for e in events]
    return sorted(items, key=lambda item: item[0])


def _is_uuid(value):
    import uuid

    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


# --- Notifications (Phase 15-A service) --------------------------------------------------------
# Customer notices never include message text or internal notes; staff notices name the
# ticket reference only.


def _staff_link(ticket):
    from apps.notifications import inbox

    return inbox.link_to("administration:support_ticket", ticket.reference)


def _customer_link(ticket):
    from apps.notifications import inbox

    return inbox.link_to("support:ticket", ticket.reference)


def _notify(recipients, event, title, message, link, key, email=False):
    from apps.notifications import inbox

    inbox.notify(recipients, f"support.{event}", category=inbox.Category.SUPPORT, title=title, message=message,
                 link=link, dedupe_key=f"support.{event}:{key}", email=email)


def _notify_created(ticket):
    _notify(ticket.requester, "received", f"We received your support request {ticket.reference}",
            "ClipCash support will reply here. You can add details by replying to the ticket.",
            _customer_link(ticket), ticket.pk)
    _notify(list(agents()), "new_ticket", f"New support ticket {ticket.reference}",
            f"Category: {ticket.get_category_display()}.", _staff_link(ticket), ticket.pk)


def _notify_assigned(ticket, actor):
    if ticket.assignee is not None and ticket.assignee_id != getattr(actor, "pk", None):
        _notify(ticket.assignee, "assigned", f"Support ticket {ticket.reference} assigned to you",
                f"Category: {ticket.get_category_display()}.", _staff_link(ticket),
                f"{ticket.pk}:{ticket.assignee_id}:{ticket.updated_at.isoformat()}")


def _notify_customer_reply(ticket, message, reopened):
    recipients = [ticket.assignee] if ticket.assignee_id else list(agents())
    title = (f"Support ticket {ticket.reference} was reopened" if reopened
             else f"Customer replied on {ticket.reference}")
    _notify(recipients, "reopened" if reopened else "customer_replied", title,
            "Open the ticket to read the reply.", _staff_link(ticket), message.pk)


def _notify_staff_reply(ticket, message):
    _notify(ticket.requester, "staff_replied", f"ClipCash support replied to {ticket.reference}",
            "Open your ticket to read the reply.", _customer_link(ticket), message.pk, email=True)


def _notify_outcome(ticket):
    resolved = ticket.status == S.RESOLVED
    title = (f"Your support ticket {ticket.reference} is resolved" if resolved
             else f"Your support ticket {ticket.reference} is closed")
    message = (f"If you still need help, reply within {settings.SUPPORT_REOPEN_DAYS} days to reopen it."
               if resolved else "Open a new ticket if you need more help.")
    _notify(ticket.requester, "resolved" if resolved else "closed", title, message, _customer_link(ticket),
            f"{ticket.pk}:{ticket.status}:{(ticket.resolved_at if resolved else ticket.closed_at).isoformat()}",
            email=resolved)
