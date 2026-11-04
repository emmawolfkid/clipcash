"""
Customer support tickets (Phase 15-B).

PostgreSQL enforces (migration 0002): tickets are created OPEN; reference,
requester, organization and creation time never change; status follows the
documented state machine (CLOSED is final); messages and history events are
append-only; nothing is deleted. Use apps.support.services for every change.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.core.models import BaseModel

MAX_SUBJECT = 150
MAX_BODY = 5000


class Ticket(BaseModel):
    class Status(models.TextChoices):
        OPEN = "OPEN", _("Open")
        IN_PROGRESS = "IN_PROGRESS", _("In progress")
        WAITING = "WAITING_FOR_CUSTOMER", _("Waiting for your reply")
        RESOLVED = "RESOLVED", _("Resolved")
        CLOSED = "CLOSED", _("Closed")

    class Category(models.TextChoices):
        ACCOUNT = "ACCOUNT", _("Account and sign-in")
        MEMBERSHIP = "MEMBERSHIP", _("Membership")
        VIDEOS = "VIDEOS", _("Videos and sponsored viewing")
        ADVERTISING = "ADVERTISING", _("Advertising and campaigns")
        DEMO = "DEMO", _("Demo rewards, wallet and withdrawals")
        TECHNICAL = "TECHNICAL", _("Technical problem")
        OTHER = "OTHER", _("Something else")

    class Priority(models.TextChoices):
        LOW = "LOW", _("Low")
        NORMAL = "NORMAL", _("Normal")
        HIGH = "HIGH", _("High")
        URGENT = "URGENT", _("Urgent")

    reference = models.CharField(_("reference"), max_length=16, unique=True)
    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="support_tickets")
    organization = models.ForeignKey("advertising.Organization", on_delete=models.PROTECT, null=True, blank=True,
                                     related_name="support_tickets")
    subject = models.CharField(_("subject"), max_length=MAX_SUBJECT)
    category = models.CharField(_("category"), max_length=12, choices=Category.choices)
    priority = models.CharField(_("priority"), max_length=8, choices=Priority.choices, default=Priority.NORMAL)
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, default=Status.OPEN)
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                 related_name="assigned_support_tickets")
    submission_key = models.CharField(_("submission key"), max_length=64)
    last_activity_at = models.DateTimeField(_("last activity"))
    resolved_at = models.DateTimeField(_("resolved at"), null=True, blank=True)
    closed_at = models.DateTimeField(_("closed at"), null=True, blank=True)

    class Meta:
        verbose_name = _("support ticket")
        verbose_name_plural = _("support tickets")
        ordering = ["-last_activity_at"]
        permissions = [
            ("view_tickets", "Can view the support queue and tickets"),
            ("manage_tickets", "Can assign, answer and change the status of support tickets"),
        ]
        indexes = [
            models.Index(fields=["status", "-last_activity_at"], name="support_queue_idx"),
            models.Index(fields=["assignee", "status"], name="support_assignee_idx"),
            models.Index(fields=["requester", "-last_activity_at"], name="support_requester_idx"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["requester", "submission_key"], name="support_ticket_submitted_once"),
            models.CheckConstraint(condition=Q(status__in=["OPEN", "IN_PROGRESS", "WAITING_FOR_CUSTOMER",
                                                           "RESOLVED", "CLOSED"]), name="support_status_valid"),
            models.CheckConstraint(condition=Q(category__in=["ACCOUNT", "MEMBERSHIP", "VIDEOS", "ADVERTISING", "DEMO",
                                                             "TECHNICAL", "OTHER"]), name="support_category_valid"),
            models.CheckConstraint(condition=Q(priority__in=["LOW", "NORMAL", "HIGH", "URGENT"]),
                                   name="support_priority_valid"),
            models.CheckConstraint(condition=~Q(subject=""), name="support_subject_not_blank"),
        ]

    def __str__(self):
        return self.reference

    @property
    def is_closed(self):
        return self.status == self.Status.CLOSED

    @property
    def staff_status_label(self):
        """The customer-facing WAITING label reads "your reply"; staff see "customer"."""
        return _("Waiting for customer") if self.status == self.Status.WAITING else self.get_status_display()


class TicketMessage(BaseModel):
    """A reply in the conversation. ``internal`` notes are visible to staff only."""

    ticket = models.ForeignKey(Ticket, on_delete=models.PROTECT, related_name="messages")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="support_messages")
    body = models.TextField(_("message"), max_length=MAX_BODY)
    internal = models.BooleanField(_("internal note"), default=False)
    from_staff = models.BooleanField(_("from staff"), default=False)
    submission_key = models.CharField(_("submission key"), max_length=64)

    class Meta:
        verbose_name = _("support message")
        verbose_name_plural = _("support messages")
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["ticket", "author", "submission_key"], name="support_message_sent_once"),
            models.CheckConstraint(condition=~Q(body=""), name="support_message_not_blank"),
            # Customers never write internal notes.
            models.CheckConstraint(condition=Q(internal=False) | Q(from_staff=True), name="support_note_by_staff"),
        ]


class TicketEvent(BaseModel):
    """History: creation, assignment, status and priority changes, reopening."""

    class Action(models.TextChoices):
        CREATED = "CREATED", _("Created")
        ASSIGNED = "ASSIGNED", _("Assigned")
        STATUS = "STATUS", _("Status changed")
        PRIORITY = "PRIORITY", _("Priority changed")
        REOPENED = "REOPENED", _("Reopened")

    ticket = models.ForeignKey(Ticket, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                              related_name="+")
    action = models.CharField(_("action"), max_length=10, choices=Action.choices)
    from_value = models.CharField(_("from"), max_length=40, blank=True)
    to_value = models.CharField(_("to"), max_length=40, blank=True)

    class Meta:
        verbose_name = _("support ticket event")
        verbose_name_plural = _("support ticket events")
        ordering = ["created_at"]

    def _label(self, value):
        choices = Ticket.Priority if self.action == self.Action.PRIORITY else Ticket.Status
        return choices(value).label if value in choices.values else value

    @property
    def from_label(self):
        return self._label(self.from_value)

    @property
    def to_label(self):
        return self._label(self.to_value)
