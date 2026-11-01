from django.utils import timezone
from django.views.generic import TemplateView

from apps.accounts.access import VerifiedAccountRequiredMixin
from apps.core import features


class MemberDashboardView(VerifiedAccountRequiredMixin, TemplateView):
    """Member home: real membership data only; future features marked Coming Soon."""

    template_name = "members/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        membership = None
        pending = None
        recent_videos = []
        if features.is_enabled("memberships"):
            from apps.memberships.access import check_access
            from apps.memberships.models import Subscription

            decision = check_access(self.request.user)
            membership = decision.subscription
            context["membership_reason"] = decision.reason
            pending = Subscription.objects.filter(member=self.request.user,
                                                  status=Subscription.Status.PENDING).first()
        if features.is_enabled("videos"):
            from apps.videos.services import library_queryset, thumbnail_url

            recent_videos = list(library_queryset().order_by("-published_at")[:4])
            for video in recent_videos:
                video.thumbnail = thumbnail_url(video)
        context.update({"nav": "dashboard", "membership": membership, "pending": pending, "recent_videos": recent_videos,
                        "now": timezone.now()})
        return context
