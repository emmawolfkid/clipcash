from django.contrib import admin

from .models import Ticket


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    """Read-only overview. Work on tickets happens in the console (/manage/support/),
    where the service layer validates, audits and notifies every change."""

    list_display = ("reference", "subject", "status", "priority", "category", "assignee", "last_activity_at")
    list_filter = ("status", "priority", "category")
    search_fields = ("reference", "subject")
    readonly_fields = [field.name for field in Ticket._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
