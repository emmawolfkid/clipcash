"""Read-only demo reward administration; every change goes through apps.rewards.services."""

from django.contrib import admin

from . import services
from .models import CampaignRewardBudget, DemoReward, RewardPolicy


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return services.can_use_rewards_console(request.user)

    def has_module_permission(self, request):
        return services.can_use_rewards_console(request.user)

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]


@admin.register(RewardPolicy)
class RewardPolicyAdmin(ReadOnlyAdmin):
    list_display = ("campaign", "version", "reward_amount", "status", "effective_from", "effective_until")
    list_filter = ("status",)


@admin.register(DemoReward)
class DemoRewardAdmin(ReadOnlyAdmin):
    list_display = ("member", "campaign", "amount", "status", "decided_at", "reversed_at")
    list_filter = ("status",)


@admin.register(CampaignRewardBudget)
class CampaignRewardBudgetAdmin(ReadOnlyAdmin):
    list_display = ("campaign", "demo_consumed", "updated_at")
