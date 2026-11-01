"""
Read-only financial ledger views.

Access requires an ACTIVE staff account holding ``wallets.view_ledger``
(Super Administrators implicitly; everyone else must be granted it explicitly).
There are no add, change, delete or posting controls: entries are written only
by apps.wallets.services.
"""

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from apps.core.money import format_amount

from .models import JournalEntry, JournalLine, LedgerAccount


def can_view_ledger(user):
    return bool(user.is_authenticated and user.is_active and user.is_staff
                and user.has_perm("wallets.view_ledger"))


class ReadOnlyLedgerAdmin(admin.ModelAdmin):
    actions = None
    list_per_page = 50
    show_full_result_count = False

    def has_module_permission(self, request):
        return can_view_ledger(request.user)

    def has_view_permission(self, request, obj=None):
        return can_view_ledger(request.user)

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields] + list(self.extra_readonly)

    extra_readonly = ()


@admin.register(LedgerAccount)
class LedgerAccountAdmin(ReadOnlyLedgerAdmin):
    list_display = ("code", "name", "account_type", "classification", "funding_class",
                    "scope_type", "status", "balance_display")
    list_filter = ("classification", "funding_class", "account_type", "scope_type", "status",
                   "overdraft_protected")
    search_fields = ("code", "name", "scope_id")
    extra_readonly = ("balance_display",)

    @admin.display(description=_("balance (normal side)"))
    def balance_display(self, obj):
        return format_amount(obj.balance, obj.currency)


class JournalLineInline(admin.TabularInline):
    model = JournalLine
    fields = ("line_number", "account", "side", "amount_display", "memo")
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = False

    @admin.display(description=_("amount"))
    def amount_display(self, obj):
        return format_amount(obj.amount, obj.entry.currency)

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return can_view_ledger(request.user)


class ReversalStateFilter(admin.SimpleListFilter):
    title = _("reversal")
    parameter_name = "reversal"

    def lookups(self, request, model_admin):
        return [("reversal", _("Is a reversal")), ("reversed", _("Has been reversed")),
                ("original", _("Not reversed"))]

    def queryset(self, request, queryset):
        if self.value() == "reversal":
            return queryset.filter(reverses__isnull=False)
        if self.value() == "reversed":
            return queryset.filter(reversed_by__isnull=False)
        if self.value() == "original":
            return queryset.filter(reverses__isnull=True, reversed_by__isnull=True)
        return queryset


@admin.register(JournalEntry)
class JournalEntryAdmin(ReadOnlyLedgerAdmin):
    list_display = ("posted_at", "posting_type", "total_display", "source_type", "source_id",
                    "external_reference", "reverses")
    list_filter = ("posting_type", "source_type", ReversalStateFilter)
    search_fields = ("external_reference", "source_id", "idempotency_key", "description", "request_id")
    date_hierarchy = "posted_at"
    inlines = [JournalLineInline]
    extra_readonly = ("total_display",)

    @admin.display(description=_("total"))
    def total_display(self, obj):
        return format_amount(obj.total_amount, obj.currency)

    def lookup_allowed(self, lookup, value, request=None):
        if lookup in ("reverses", "related_entry", "actor_id"):
            return True
        return super().lookup_allowed(lookup, value, request)


@admin.register(JournalLine)
class JournalLineAdmin(ReadOnlyLedgerAdmin):
    list_display = ("entry", "line_number", "account", "side", "amount_display")
    list_filter = ("side", "account__account_type", "account__funding_class")
    search_fields = ("account__code", "entry__external_reference", "entry__source_id")
    list_select_related = ("entry", "account")
    extra_readonly = ("amount_display",)

    @admin.display(description=_("amount"))
    def amount_display(self, obj):
        return format_amount(obj.amount, obj.entry.currency)
