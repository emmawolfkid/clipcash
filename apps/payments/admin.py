"""Read-only payments administration; all changes go through apps.payments.services."""

from django.contrib import admin

from . import services
from .models import PaymentIntent, ReconciliationException, SandboxPosting


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return services.can_view_payments(request.user)

    def has_module_permission(self, request):
        return services.can_view_payments(request.user)

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]


@admin.register(PaymentIntent)
class PaymentIntentAdmin(ReadOnlyAdmin):
    list_display = ("reference", "purpose", "amount", "status", "environment", "created_at")
    list_filter = ("status", "purpose")
    search_fields = ("reference", "provider_reference")


@admin.register(ReconciliationException)
class ReconciliationExceptionAdmin(ReadOnlyAdmin):
    list_display = ("reason", "status", "amount", "created_at")
    list_filter = ("status", "reason")


@admin.register(SandboxPosting)
class SandboxPostingAdmin(ReadOnlyAdmin):
    list_display = ("posting_type", "debit_account", "credit_account", "amount", "funds_domain", "posted_at")
    list_filter = ("posting_type", "funds_domain")
