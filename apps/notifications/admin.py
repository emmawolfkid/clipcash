"""Read-only email delivery status. No bodies, links or tokens exist to show."""

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from apps.administration.admin import OperationsReadOnlyAdmin

from .models import Notification, OutboundEmail


@admin.register(OutboundEmail)
class OutboundEmailAdmin(OperationsReadOnlyAdmin):
    list_display = ("created_at", "kind", "masked_recipient", "status", "attempts", "expires_at",
                    "sent_at", "last_error_class", "skip_reason")
    list_filter = ("status", "kind", "last_error_class")
    date_hierarchy = "created_at"
    actions = None
    fields = ("id", "kind", "masked_recipient", "user_id", "status", "attempts", "token_issued_at",
              "expires_at", "sent_at", "last_error_class", "skip_reason", "created_at", "updated_at")

    @admin.display(description=_("recipient"))
    def masked_recipient(self, obj):
        return obj.masked_recipient

    def get_readonly_fields(self, request, obj=None):
        return self.fields


@admin.register(Notification)
class NotificationAdmin(OperationsReadOnlyAdmin):
    """Read-only, Super Administrators only: notices can mention members' own activity."""

    list_display = ("created_at", "category", "kind", "recipient", "read_at")
    list_filter = ("category", "kind")
    date_hierarchy = "created_at"
    actions = None
    fields = ("id", "recipient", "category", "kind", "title", "message", "link", "dedupe_key", "read_at",
              "created_at")

    def get_readonly_fields(self, request, obj=None):
        return self.fields

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser
