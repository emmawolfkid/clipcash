"""
Read-only advertising administration. Every change goes through the reviewed
workflows in the administration console (/manage/advertisers/, /manage/campaigns/)
and apps.advertising.services; nothing can be added, edited or deleted here.
"""

from django.contrib import admin

from . import services
from .models import AdvertiserMembership, AdvertiserVideo, Campaign, Organization


class ReadOnlyAdmin(admin.ModelAdmin):
    review_check = staticmethod(services.can_review_advertisers)

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return self.review_check(request.user)

    def has_module_permission(self, request):
        return services.can_use_advertising_console(request.user)

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]


@admin.register(Organization)
class OrganizationAdmin(ReadOnlyAdmin):
    list_display = ("display_name", "legal_name", "country", "verification_status", "submitted_at", "reviewed_at")
    list_filter = ("verification_status", "country")
    search_fields = ("display_name", "legal_name")


@admin.register(AdvertiserMembership)
class AdvertiserMembershipAdmin(ReadOnlyAdmin):
    list_display = ("organization", "user", "role", "is_active", "created_at")
    list_filter = ("role", "is_active")


@admin.register(AdvertiserVideo)
class AdvertiserVideoAdmin(ReadOnlyAdmin):
    list_display = ("organization", "video", "granted_by", "created_at", "revoked_at")


@admin.register(Campaign)
class CampaignAdmin(ReadOnlyAdmin):
    review_check = staticmethod(services.can_review_campaigns)
    list_display = ("name", "organization", "status", "starts_at", "ends_at", "demo_budget_amount", "funding_status",
                    "revision")
    list_filter = ("status", "funding_status")
    search_fields = ("name", "organization__display_name")
