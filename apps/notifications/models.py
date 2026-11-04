from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.core.models import BaseModel


class OutboundEmail(BaseModel):
    """
    Durable email outbox entry.

    Deliberately stores no rendered body, link or token: only the message kind,
    recipient, subject account and non-sensitive context. Links and tokens are
    generated when the message is sent, dated to ``token_issued_at``, so retries
    can never extend a token's validity and nothing secret rests in the
    database, admin, job payloads or logs.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        SENT = "SENT", _("Sent")
        FAILED = "FAILED", _("Failed")
        EXPIRED = "EXPIRED", _("Expired before delivery")
        SKIPPED = "SKIPPED", _("No longer applicable")
        CANCELLED = "CANCELLED", _("Cancelled")

    kind = models.CharField(_("kind"), max_length=64)
    recipient = models.EmailField(_("recipient"))
    user_id = models.UUIDField(_("account"), null=True, blank=True)
    context = models.JSONField(_("context"), default=dict, blank=True)
    status = models.CharField(_("status"), max_length=10, choices=Status.choices, default=Status.PENDING)
    token_issued_at = models.DateTimeField(_("requested at"))
    expires_at = models.DateTimeField(_("expires at"))
    sent_at = models.DateTimeField(_("sent at"), null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(_("delivery attempts"), default=0)
    last_error_class = models.CharField(_("error class"), max_length=40, blank=True)
    skip_reason = models.CharField(_("skip reason"), max_length=60, blank=True)

    class Meta:
        verbose_name = _("outbound email")
        verbose_name_plural = _("outbound emails")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="outbox_status_idx"),
            models.Index(fields=["kind", "-created_at"], name="outbox_kind_idx"),
            models.Index(fields=["user_id"], name="outbox_user_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["PENDING", "SENT", "FAILED", "EXPIRED", "SKIPPED", "CANCELLED"]),
                name="outbox_status_valid"),
            models.CheckConstraint(condition=Q(expires_at__gt=models.F("token_issued_at")),
                                   name="outbox_expiry_after_issue"),
            models.CheckConstraint(condition=~Q(status="SENT") | Q(sent_at__isnull=False),
                                   name="outbox_sent_has_time"),
        ]

    def __str__(self):
        return f"{self.kind} [{self.status}]"

    @property
    def masked_recipient(self):
        local, _, domain = self.recipient.partition("@")
        return f"{local[:1]}***@{domain}" if domain else "***"


class Notification(BaseModel):
    """
    One in-app notice for one account (Phase 15-A).

    Written by the authoritative business service inside its own transaction, so a
    notice exists only if the change it describes was committed. ``dedupe_key`` is
    unique per recipient: retries, repeated jobs and replayed requests cannot create
    a second notice for the same event. ``link`` is an internal path only (database
    rule), never an external URL. Text is written for the recipient and carries no
    secrets, tokens or other members' personal data.
    """

    class Category(models.TextChoices):
        ACCOUNT = "ACCOUNT", _("Account and security")
        MEMBERSHIP = "MEMBERSHIP", _("Membership")
        ADVERTISING = "ADVERTISING", _("Advertising")
        VIDEO = "VIDEO", _("Videos")
        VIEWING = "VIEWING", _("Sponsored viewing")
        REWARD = "REWARD", _("Demo rewards")
        WITHDRAWAL = "WITHDRAWAL", _("Demo withdrawals")
        OPERATIONS = "OPERATIONS", _("Operations")

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    category = models.CharField(_("category"), max_length=12, choices=Category.choices)
    kind = models.CharField(_("event"), max_length=64)
    title = models.CharField(_("title"), max_length=120)
    message = models.CharField(_("message"), max_length=400, blank=True)
    link = models.CharField(_("link"), max_length=200, blank=True)
    dedupe_key = models.CharField(_("event key"), max_length=160)
    read_at = models.DateTimeField(_("read at"), null=True, blank=True)

    class Meta:
        verbose_name = _("notification")
        verbose_name_plural = _("notifications")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "-created_at"], name="notification_recipient_idx"),
            models.Index(fields=["recipient"], condition=Q(read_at__isnull=True), name="notification_unread_idx"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["recipient", "dedupe_key"], name="notification_once_per_event"),
            models.CheckConstraint(
                condition=Q(category__in=["ACCOUNT", "MEMBERSHIP", "ADVERTISING", "VIDEO", "VIEWING", "REWARD",
                                          "WITHDRAWAL", "OPERATIONS"]),
                name="notification_category_valid"),
            # Internal paths only: "" or "/..." but never "//host" (protocol-relative) or a URL.
            models.CheckConstraint(condition=Q(link="") | (Q(link__startswith="/") & ~Q(link__startswith="//")
                                                            & ~Q(link__contains="\\")),
                                   name="notification_link_internal"),
            models.CheckConstraint(condition=~Q(title=""), name="notification_title_not_blank"),
        ]

    def __str__(self):
        return f"{self.kind} -> {self.recipient_id}"

    @property
    def is_read(self):
        return self.read_at is not None
