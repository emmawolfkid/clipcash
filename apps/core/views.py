from django.http import HttpResponseRedirect
from django.templatetags.static import static
from django.views.decorators.http import require_safe
from django.views.generic import TemplateView

from apps.accounts.access import VerifiedAccountRequiredMixin
from apps.core import features

from .dashboard import member_activity, member_summary


@require_safe
def favicon(request):
    """FAV-01: /favicon.ico, which browsers and other clients request by convention (and for
    pages without icon links), answered with the collected, content-hashed ICO. A temporary
    redirect, cached for a day, because the hashed name changes when the icon does."""
    response = HttpResponseRedirect(static("images/favicon.ico"))
    response["Cache-Control"] = "public, max-age=86400"
    return response


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
        videos_locked = False
        if features.is_enabled("videos"):
            from apps.videos.services import catalogue_access, member_catalogue, thumbnail_url

            # PAY-LIVE-01B: the same rule as the video library; no titles or signed
            # thumbnails for members without a current membership.
            access = catalogue_access(self.request.user)
            videos_locked = not access
            if access:
                recent_videos = list(member_catalogue(access.subscription).order_by("-published_at")[:4])
                for video in recent_videos:
                    video.thumbnail = thumbnail_url(video)
        from apps.accounts.services import email_change_available

        context.update({"nav": "dashboard", "summary": summary, "recent_videos": recent_videos,
                        "videos_locked": videos_locked,
                        # REG-04: the unconfirmed-email reminder offers a way to correct the address.
                        "email_change_available": email_change_available(self.request.user),
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
