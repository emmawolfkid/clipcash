"""
Read-only audit workspace.

Access requires an ACTIVE staff account holding ``audit.view_auditevent``
(superusers hold it implicitly; others must be granted it explicitly by a
superuser). Nobody can add, change or delete events here.
"""

import json

from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from .events import EVENT_TYPES
from .models import AuditEvent


def can_view_audit(user):
    return bool(
        user.is_authenticated
        and user.is_active
        and user.is_staff
        and user.has_perm("audit.view_auditevent")
    )


class ActionFilter(admin.SimpleListFilter):
    title = _("action")
    parameter_name = "action"

    def lookups(self, request, model_admin):
        return [(action, action) for action in sorted(EVENT_TYPES)]

    def queryset(self, request, queryset):
        return queryset.filter(action=self.value()) if self.value() else queryset


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "action", "outcome", "actor_display", "target_display", "source")
    list_filter = ("outcome", "category", "source", ActionFilter, "actor_role", "target_model")
    search_fields = ("actor_email", "target_id", "target_repr", "request_id")
    search_help_text = _("Search by actor email, target id or name, or request id.")
    date_hierarchy = "occurred_at"
    list_per_page = 50
    list_max_show_all = 200
    show_full_result_count = False  # Avoid a full COUNT(*) on a large table.
    actions = None
    fieldsets = (
        (None, {"fields": ("id", "occurred_at", "action", "category", "outcome", "source")}),
        (_("Actor"), {"fields": ("actor_id", "actor_email", "actor_role")}),
        (_("Target"), {"fields": ("target_app_label", "target_model", "target_id", "target_repr")}),
        (_("Request"), {"fields": ("request_id", "ip_address", "user_agent")}),
        (_("Details"), {"fields": ("metadata_display",)}),
    )
    readonly_fields = (
        "id", "occurred_at", "action", "category", "outcome", "source",
        "actor_id", "actor_email", "actor_role",
        "target_app_label", "target_model", "target_id", "target_repr",
        "request_id", "ip_address", "user_agent", "metadata_display",
    )

    # Filtering by exact actor or target id via the URL (?actor_id=...).
    def lookup_allowed(self, lookup, value, request=None):
        if lookup in ("actor_id", "target_id", "request_id"):
            return True
        return super().lookup_allowed(lookup, value, request)

    @admin.display(description=_("actor"), ordering="actor_email")
    def actor_display(self, obj):
        if not obj.actor_id:
            return "—"
        return f"{obj.actor_email or obj.actor_id} ({obj.actor_role})" if obj.actor_role else obj.actor_email

    @admin.display(description=_("target"))
    def target_display(self, obj):
        if not obj.target_model:
            return "—"
        return f"{obj.target_model}: {obj.target_repr or obj.target_id}"

    @admin.display(description=_("metadata"))
    def metadata_display(self, obj):
        return format_html("<pre>{}</pre>", json.dumps(obj.metadata, indent=2, sort_keys=True))

    def has_module_permission(self, request):
        return can_view_audit(request.user)

    def has_view_permission(self, request, obj=None):
        return can_view_audit(request.user)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
