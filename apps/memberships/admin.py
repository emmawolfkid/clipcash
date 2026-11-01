"""
Membership administration.

* Plans: Super Administrators only. Saves go through services (versioning and
  audit); code is fixed after creation; no deletion (archive instead).
* Subscriptions: read-only. Suspend / reinstate / cancel require
  memberships.manage_memberships (Super Administrators implicitly), a reason,
  and go through the lifecycle services. There is no way to activate or mark
  a subscription paid here.
"""

from datetime import timedelta

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.core.exceptions import PermissionDenied
from django.template.response import TemplateResponse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.money import format_amount

from . import services
from .forms import MembershipPlanForm, ReasonForm
from .models import MembershipPlan, MembershipPlanVersion, Subscription


class PlanVersionInline(admin.TabularInline):
    model = MembershipPlanVersion
    fields = ("version", "name", "price_display", "duration_days", "entitlements", "created_at")
    readonly_fields = fields
    extra = 0
    can_delete = False
    ordering = ["-version"]

    @admin.display(description=_("price"))
    def price_display(self, obj):
        return format_amount(obj.price_amount, obj.currency)

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return services.is_super_admin(request.user)


@admin.register(MembershipPlan)
class MembershipPlanAdmin(admin.ModelAdmin):
    form = MembershipPlanForm
    list_display = ("name", "code", "price_display", "duration_days", "status", "is_public", "display_order",
                    "version")
    list_filter = ("status", "is_public")
    search_fields = ("name", "code")
    inlines = [PlanVersionInline]
    actions = ["activate_plans", "deactivate_plans", "archive_plans"]
    readonly_fields = ("status", "version", "created_at", "updated_at")

    @admin.display(description=_("price"), ordering="price_amount")
    def price_display(self, obj):
        return format_amount(obj.price_amount, obj.currency)

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields + (("code",) if obj is not None else ())

    # Super Administrators only (server-side, not just hidden).
    def has_module_permission(self, request):
        return services.is_super_admin(request.user)

    def has_view_permission(self, request, obj=None):
        return services.is_super_admin(request.user)

    def has_add_permission(self, request):
        return services.is_super_admin(request.user)

    def has_change_permission(self, request, obj=None):
        return services.is_super_admin(request.user) and (obj is None or obj.status != MembershipPlan.Status.ARCHIVED)

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        values = form.plan_values()
        if change:
            original = MembershipPlan.objects.get(pk=obj.pk)
            changes = {k: v for k, v in values.items() if getattr(original, k) != v}
            saved = services.update_plan(request.user, original, changes)
        else:
            saved = services.create_plan(request.user, code=form.cleaned_data["code"], **values)
        obj.pk, obj.version, obj._state.adding = saved.pk, saved.version, False

    def _set_status(self, request, queryset, status):
        changed = 0
        for plan in queryset:
            try:
                services.set_plan_status(request.user, plan, status)
                changed += 1
            except services.MembershipError as error:
                self.message_user(request, f"{plan.code}: {error}", level=messages.WARNING)
        self.message_user(request, _("%(n)s plan(s) updated.") % {"n": changed})

    @admin.action(description=_("Make selected plans active"))
    def activate_plans(self, request, queryset):
        self._set_status(request, queryset, MembershipPlan.Status.ACTIVE)

    @admin.action(description=_("Deactivate selected plans (hidden, not purchasable)"))
    def deactivate_plans(self, request, queryset):
        self._set_status(request, queryset, MembershipPlan.Status.INACTIVE)

    @admin.action(description=_("Archive selected plans (permanent)"))
    def archive_plans(self, request, queryset):
        self._set_status(request, queryset, MembershipPlan.Status.ARCHIVED)


class ExpiringSoonFilter(admin.SimpleListFilter):
    title = _("expiry")
    parameter_name = "expiring"

    def lookups(self, request, model_admin):
        return [("soon", _("Expiring within %(n)s days") % {"n": settings.MEMBERSHIPS_EXPIRING_SOON_DAYS}),
                ("ended", _("Period ended, not yet processed"))]

    def queryset(self, request, queryset):
        now = timezone.now()
        if self.value() == "soon":
            return queryset.filter(status=Subscription.Status.ACTIVE, expires_at__gt=now,
                                   expires_at__lte=now + timedelta(days=settings.MEMBERSHIPS_EXPIRING_SOON_DAYS))
        if self.value() == "ended":
            return queryset.filter(status__in=[Subscription.Status.ACTIVE, Subscription.Status.SUSPENDED],
                                   expires_at__lte=now)
        return queryset


def can_view_subscriptions(user):
    return services.is_super_admin(user) or bool(
        user.is_authenticated and user.is_active and user.is_staff and user.has_perm("memberships.view_memberships"))


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("member", "plan_name", "status", "price_display", "starts_at", "expires_at",
                    "activation_source", "payment_reference")
    list_filter = ("status", "activation_source", ExpiringSoonFilter, "plan")
    search_fields = ("member__email", "payment_reference", "plan_name")
    list_select_related = ("member", "plan")
    date_hierarchy = "created_at"
    show_full_result_count = False
    actions = ["suspend_selected", "reinstate_selected", "cancel_selected"]

    @admin.display(description=_("price"), ordering="price_amount")
    def price_display(self, obj):
        return format_amount(obj.price_amount, obj.currency)

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_module_permission(self, request):
        return can_view_subscriptions(request.user)

    def has_view_permission(self, request, obj=None):
        return can_view_subscriptions(request.user)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_manage_permission(self, request):
        return can_view_subscriptions(request.user) and services.can_manage_subscriptions(request.user)

    def _with_reason(self, request, queryset, title, apply):
        if not self.has_manage_permission(request):
            raise PermissionDenied
        form = ReasonForm(request.POST if "apply" in request.POST else None)
        if form.is_bound and form.is_valid():
            done, refused = 0, 0
            for subscription in queryset:
                try:
                    apply(subscription, form.cleaned_data["reason"])
                    done += 1
                except services.MembershipError:
                    refused += 1
            self.message_user(request, _("%(n)s subscription(s) updated.") % {"n": done})
            if refused:
                self.message_user(request, _("%(n)s were not eligible.") % {"n": refused}, level=messages.WARNING)
            return None
        return TemplateResponse(request, "admin/memberships/subscription/reason.html", {
            **self.admin_site.each_context(request), "title": title, "form": form, "queryset": queryset,
            "opts": self.model._meta, "action": request.POST.get("action"),
            "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
        })

    @admin.action(description=_("Suspend selected memberships"), permissions=["manage"])
    def suspend_selected(self, request, queryset):
        return self._with_reason(request, queryset, _("Suspend memberships"),
                                 lambda s, reason: services.suspend(request.user, s, reason))

    @admin.action(description=_("Cancel selected memberships (immediate)"), permissions=["manage"])
    def cancel_selected(self, request, queryset):
        return self._with_reason(request, queryset, _("Cancel memberships"),
                                 lambda s, reason: services.cancel(request.user, s, reason))

    @admin.action(description=_("Reinstate selected suspended memberships"), permissions=["manage"])
    def reinstate_selected(self, request, queryset):
        done = 0
        for subscription in queryset:
            try:
                services.reinstate(request.user, subscription)
                done += 1
            except services.MembershipError:
                pass
        self.message_user(request, _("%(n)s subscription(s) reinstated.") % {"n": done})
