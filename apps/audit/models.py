from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import UUIDModel

from .events import Category


class AuditImmutableError(RuntimeError):
    """Raised on any attempt to change or delete an audit event through the ORM."""


class AuditEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise AuditImmutableError("Audit events are append-only.")

    def delete(self):
        raise AuditImmutableError("Audit events are append-only; use the audit_purge command.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise AuditImmutableError("Audit events are append-only.")

    def update_or_create(self, *args, **kwargs):
        raise AuditImmutableError("Audit events are append-only.")


class AuditEvent(UUIDModel):
    """
    One immutable audit record.

    Actor and target are stored as identifiers plus snapshots, not foreign keys,
    so records stay meaningful after accounts or objects are deleted and can
    never be cascade-deleted. Metadata is validated against the event registry.
    """

    class Outcome(models.TextChoices):
        SUCCESS = "SUCCESS", _("Success")
        FAILURE = "FAILURE", _("Failure")
        DENIED = "DENIED", _("Denied")

    class Source(models.TextChoices):
        WEB = "WEB", _("Web")
        ADMIN = "ADMIN", _("Admin")
        SYSTEM = "SYSTEM", _("System")
        CLI = "CLI", _("Command line")

    occurred_at = models.DateTimeField(_("occurred at"), default=timezone.now, editable=False)
    category = models.CharField(_("category"), max_length=20, choices=Category.choices)
    action = models.CharField(_("action"), max_length=64)
    outcome = models.CharField(_("outcome"), max_length=10, choices=Outcome.choices)
    source = models.CharField(_("source"), max_length=10, choices=Source.choices)

    actor_id = models.UUIDField(_("actor id"), null=True, blank=True)
    actor_email = models.CharField(_("actor email"), max_length=254, blank=True)
    actor_role = models.CharField(_("actor role"), max_length=20, blank=True)

    target_app_label = models.CharField(_("target app"), max_length=100, blank=True)
    target_model = models.CharField(_("target model"), max_length=100, blank=True)
    target_id = models.CharField(_("target id"), max_length=64, blank=True)
    target_repr = models.CharField(_("target"), max_length=200, blank=True)

    request_id = models.CharField(_("request id"), max_length=64, blank=True)
    ip_address = models.GenericIPAddressField(_("IP address"), null=True, blank=True)
    user_agent = models.CharField(_("user agent"), max_length=256, blank=True)

    metadata = models.JSONField(_("metadata"), default=dict, blank=True)
    idempotency_key = models.CharField(_("idempotency key"), max_length=128, null=True, blank=True)

    objects = AuditEventQuerySet.as_manager()

    class Meta:
        verbose_name = _("audit event")
        verbose_name_plural = _("audit events")
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["-occurred_at"], name="audit_occurred_idx"),
            models.Index(fields=["actor_id", "-occurred_at"], name="audit_actor_idx"),
            models.Index(fields=["action", "-occurred_at"], name="audit_action_idx"),
            models.Index(fields=["outcome", "-occurred_at"], name="audit_outcome_idx"),
            models.Index(fields=["target_model", "target_id"], name="audit_target_idx"),
            models.Index(fields=["request_id"], name="audit_request_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="audit_idempotency_key_unique",
            ),
            models.CheckConstraint(condition=Q(action__regex=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$"),
                                   name="audit_action_format"),
            models.CheckConstraint(condition=Q(outcome__in=["SUCCESS", "FAILURE", "DENIED"]),
                                   name="audit_outcome_valid"),
            models.CheckConstraint(condition=Q(source__in=["WEB", "ADMIN", "SYSTEM", "CLI"]),
                                   name="audit_source_valid"),
            models.CheckConstraint(condition=Q(category__in=[c for c, _ in Category.choices]),
                                   name="audit_category_valid"),
        ]
        permissions = []  # Django's default view_auditevent grants read access.

    def __str__(self):
        return f"{self.occurred_at:%Y-%m-%d %H:%M:%S} {self.action} {self.outcome}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AuditImmutableError("Audit events are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditImmutableError("Audit events are append-only; use the audit_purge command.")

    def clean(self):
        super().clean()
        if not isinstance(self.metadata, dict):
            raise ValidationError({"metadata": "Metadata must be an object."})
