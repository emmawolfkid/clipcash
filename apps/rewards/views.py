"""Member demo reward pages (/rewards/). Only the signed-in member's own records."""

from django.conf import settings
from django.core.paginator import Paginator
from django.shortcuts import render
from django.utils.decorators import method_decorator
from django.views import View

from apps.accounts.access import VerifiedAccountRequiredMixin
from apps.core.features import feature_required

from . import services


@method_decorator(feature_required("advertisers"), name="dispatch")
class DemoRewardHistoryView(VerifiedAccountRequiredMixin, View):
    def get(self, request):
        page = Paginator(services.member_rewards(request.user), settings.VIEWING_HISTORY_PAGE_SIZE).get_page(
            request.GET.get("page"))
        for reward in page.object_list:
            reward.reason_text = services.reason_text(reward.reasons)
        return render(request, "rewards/history.html", {
            "nav": "rewards", "page": page, "totals": services.member_totals(request.user)})
