from django.views.generic import TemplateView

from apps.accounts.access import VerifiedAccountRequiredMixin
from apps.core import features

from .dashboard import member_activity, member_summary


class HomeView(TemplateView):
    """Public homepage. Read-only: links only, no forms beyond the site header.
    Registration links follow features.is_enabled("registration"), which the
    register view also enforces server-side. ``restricted_demo`` and
    ``financial_locked`` come from apps.core.context_processors.site."""

    template_name = "public/home.html"


class MemberDashboardView(VerifiedAccountRequiredMixin, TemplateView):
    """Member home: read-only summary cards built from the domain services."""

    template_name = "members/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        summary = member_summary(self.request.user)
        recent_videos = []
        if features.is_enabled("videos"):
            from apps.videos.services import library_queryset, thumbnail_url

            recent_videos = list(library_queryset().order_by("-published_at")[:4])
            for video in recent_videos:
                video.thumbnail = thumbnail_url(video)
        context.update({"nav": "dashboard", "summary": summary, "recent_videos": recent_videos,
                        # Kept for templates and tests that read them directly.
                        "membership": summary.membership.get("subscription"),
                        "pending": summary.membership.get("pending")})
        return context


class MemberActivityView(VerifiedAccountRequiredMixin, TemplateView):
    """The member's own recent activity (safe summaries, newest first, bounded)."""

    template_name = "members/activity.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({"nav": "activity", "activity": member_activity(self.request.user),
                        "breadcrumbs": [("Dashboard", "dashboard")]})
        return context
