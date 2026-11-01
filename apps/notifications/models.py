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
