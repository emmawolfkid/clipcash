"""
Administration foundation: admin branding and platform configuration.

Authorization is enforced server-side: the has_*_permission() checks refuse
requests (not just hide links), and every save goes through
apps.core.services.update_site_configuration(), which re-checks the actor,
locks the row and rejects stale versions.
"""

from django import forms
from django.conf import settings
from django.contrib import admin
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from apps.core import services
from apps.core.features import FEATURES
from apps.core.models import SITE_CONFIGURATION_ID, SiteConfiguration

admin.site.site_header = _("ClipCash administration")
admin.site.site_title = _("ClipCash administration")
admin.site.index_title = _("Administration")


class SuperAdminOnlyMixin:
    """Reusable: only ACTIVE Super Administrators may see or change the model."""

    def has_module_permission(self, request):
        return services.can_change_site_configuration(request.user)

    def has_view_permission(self, request, obj=None):
        return services.can_change_site_configuration(request.user)

    def has_change_permission(self, request, obj=None):
        return services.can_change_site_configuration(request.user)


class SiteConfigurationForm(forms.ModelForm):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)

    class Meta:
        model = SiteConfiguration
        fields = sorted(services.EDITABLE_FIELDS)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial["expected_version"] = self.instance.version
        if settings.RESTRICTED_DEMO_MODE and "registration_enabled" in self.fields:
            self.fields["registration_enabled"].help_text = _(
                "No effect while restricted demo mode (DJANGO_RESTRICTED_DEMO) is on: public registration stays closed."
            )

    def clean(self):
        cleaned_data = super().clean()
        # The admin change view runs in a transaction, so this lock is held until
        # the save commits: no other editor can slip in between check and save.
        current = (
            SiteConfiguration.objects.select_for_update()
            .filter(id=SITE_CONFIGURATION_ID)
            .values_list("version", flat=True)
            .first()
        )
        if cleaned_data.get("expected_version") != current:
            raise ValidationError(
                _("The configuration was changed by someone else. Reload the page and try again."),
                code="stale",
            )
        return cleaned_data


@admin.register(SiteConfiguration)
class SiteConfigurationAdmin(SuperAdminOnlyMixin, admin.ModelAdmin):
    form = SiteConfigurationForm
    readonly_fields = ("financial_features", "version", "updated_by", "updated_at")
    fieldsets = (
        (_("Identity"), {"fields": ("platform_name", "platform_description", "website_url")}),
        (_("Company and support"), {
            "fields": ("company_name", "company_address", "support_email", "support_phone"),
        }),
        (_("Regional defaults"), {
            "fields": ("default_timezone", "default_currency", "default_language"),
        }),
        (_("Availability"), {
            "fields": (
                "maintenance_mode",
                "maintenance_message",
                "registration_enabled",
                "memberships_enabled",
                "videos_enabled",
                "advertisers_enabled",
                "financial_features",
            ),
        }),
        (_("Change tracking"), {"fields": ("version", "updated_by", "updated_at", "expected_version")}),
    )

    def has_add_permission(self, request):
        return False  # The single row is created by migration.

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description=_("Financial features"))
    def financial_features(self, obj):
        names = ", ".join(str(f.label) for f in FEATURES.values() if f.financial)
        return _("Locked off: %(names)s. They cannot be enabled in this phase.") % {"names": names}

    def changelist_view(self, request, extra_context=None):
        # There is exactly one row: go straight to it.
        if not self.has_view_permission(request):
            raise PermissionDenied
        return redirect(reverse("admin:core_siteconfiguration_change", args=[SITE_CONFIGURATION_ID]))

    def save_model(self, request, obj, form, change):
        changes = {
            name: form.cleaned_data[name]
            for name in form.changed_data
            if name in services.EDITABLE_FIELDS
        }
        try:
            saved = services.update_site_configuration(
                request.user,
                expected_version=form.cleaned_data["expected_version"],
                changes=changes,
            )
        except PermissionError:
            raise PermissionDenied
        obj.version = saved.version


# --- Operations workspace (background jobs, workers, schedules) -------------------
#
# Viewing needs core.view_operations; retrying, cancelling and schedule changes
# each need their own permission and are validated and audited by services.
# There is no way to create or execute arbitrary jobs here.

from django.contrib import messages  # noqa: E402
from django.db import transaction  # noqa: E402

from apps.audit.services import record_business_event  # noqa: E402
from apps.core.jobs import services as job_services  # noqa: E402
from apps.core.models import Job, ScheduledTask, WorkerHeartbeat  # noqa: E402


def can_view_operations(user):
    return bool(user.is_authenticated and user.is_active and user.is_staff
                and user.has_perm("core.view_operations"))


class OperationsReadOnlyAdmin(admin.ModelAdmin):
    list_per_page = 50
    show_full_result_count = False

    def has_module_permission(self, request):
        return can_view_operations(request.user)

    def has_view_permission(self, request, obj=None):
        return can_view_operations(request.user)

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]


@admin.register(Job)
class JobAdmin(OperationsReadOnlyAdmin):
    list_display = ("created_at", "job_type", "status", "attempts_display", "run_after",
                    "worker_id", "last_error_class", "last_error")
    list_filter = ("status", "job_type", "last_error_class")
    search_fields = ("job_type", "worker_id", "request_id", "idempotency_key")
    date_hierarchy = "created_at"
    actions = ["retry_selected", "cancel_selected"]

    @admin.display(description=_("attempts"))
    def attempts_display(self, obj):
        return f"{obj.attempts}/{obj.max_attempts}"

    def has_retry_permission(self, request):
        return can_view_operations(request.user) and request.user.has_perm("core.retry_job")

    def has_cancel_permission(self, request):
        return can_view_operations(request.user) and request.user.has_perm("core.cancel_job")

    def _apply(self, request, queryset, action, verb):
        done, refused = 0, 0
        for job in queryset:
            try:
                action(job, actor=request.user)
                done += 1
            except job_services.JobActionError:
                refused += 1
        if done:
            self.message_user(request, _("%(count)s job(s) %(verb)s.") % {"count": done, "verb": verb})
        if refused:
            self.message_user(request, _("%(count)s job(s) were not eligible.") % {"count": refused},
                              level=messages.WARNING)

    @admin.action(description=_("Retry selected failed or cancelled jobs"), permissions=["retry"])
    def retry_selected(self, request, queryset):
        self._apply(request, queryset, job_services.retry_job, _("queued for retry"))

    @admin.action(description=_("Cancel selected pending or failed jobs"), permissions=["cancel"])
    def cancel_selected(self, request, queryset):
        self._apply(request, queryset, job_services.cancel_job, _("cancelled"))


@admin.register(WorkerHeartbeat)
class WorkerHeartbeatAdmin(OperationsReadOnlyAdmin):
    list_display = ("worker_id", "last_seen_at", "last_run_seconds", "last_processed", "last_succeeded",
                    "last_failed", "total_processed", "cache_ok", "cache_checked_at")
    actions = None


@admin.register(ScheduledTask)
class ScheduledTaskAdmin(OperationsReadOnlyAdmin):
    list_display = ("name", "enabled", "next_run_at", "last_enqueued_at")
    actions = ["enable_selected", "disable_selected"]

    def has_manage_schedules_permission(self, request):
        return can_view_operations(request.user) and request.user.has_perm("core.manage_schedules")

    def _set_enabled(self, request, queryset, enabled):
        changed = 0
        with transaction.atomic():
            for task in queryset.select_for_update():
                if task.enabled == enabled:
                    continue
                task.enabled = enabled
                task.save(update_fields=["enabled"])
                record_business_event("jobs.schedule.changed", actor=request.user, target=task,
                                      metadata={"schedule": task.name, "enabled": enabled})
                changed += 1
        self.message_user(request, _("%(count)s scheduled task(s) updated.") % {"count": changed})

    @admin.action(description=_("Enable selected scheduled tasks"), permissions=["manage_schedules"])
    def enable_selected(self, request, queryset):
        self._set_enabled(request, queryset, True)

    @admin.action(description=_("Pause selected scheduled tasks"), permissions=["manage_schedules"])
    def disable_selected(self, request, queryset):
        self._set_enabled(request, queryset, False)
