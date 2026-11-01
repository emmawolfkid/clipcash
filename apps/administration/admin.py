"""
Administration foundation: admin branding and platform configuration.

Authorization is enforced server-side: the has_*_permission() checks refuse
requests (not just hide links), and every save goes through
apps.core.services.update_site_configuration(), which re-checks the actor,
locks the row and rejects stale versions.
"""

from django import forms
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
