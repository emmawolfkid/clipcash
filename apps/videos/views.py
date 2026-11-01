from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, render
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.core.features import feature_required

from . import services
from .models import VideoCategory

REASON_MESSAGES = {
    "not_authenticated": _("Sign in to watch this video."),
    "account_inactive": _("Your account cannot play videos at the moment."),
    "email_unverified": _("Confirm your email address to watch videos."),
    "no_membership": _("An active membership is needed to watch this video."),
    "membership_suspended": _("Your membership is suspended. Please contact support."),
    "entitlement_missing": _("Your membership plan does not include this video."),
    "feature_disabled": _("Memberships are not available at the moment."),
    "unavailable": _("This video is not available right now."),
}


def _with_thumbnails(videos):
    for video in videos:
        video.thumbnail = services.thumbnail_url(video)
    return videos


@method_decorator(feature_required("videos"), name="dispatch")
class LibraryView(View):
    template_name = "videos/library.html"

    def get(self, request, slug=None):
        categories = list(VideoCategory.objects.filter(is_active=True).order_by("display_order", "name"))
        category = get_object_or_404(VideoCategory, slug=slug, is_active=True) if slug else None
        videos = services.library_queryset()
        if category is not None:
            videos = videos.filter(category=category)
        query = request.GET.get("q", "").strip()[:100]
        if query:
            videos = videos.filter(Q(title__icontains=query) | Q(description__icontains=query))
        page = Paginator(videos.order_by("-published_at", "title"), settings.VIDEO_LIBRARY_PAGE_SIZE).get_page(
            request.GET.get("page"))
        _with_thumbnails(page.object_list)
        return render(request, self.template_name, {
            "categories": categories, "category": category, "query": query, "page": page,
            "videos": page.object_list, "nav": "videos",
        })


@method_decorator(feature_required("videos"), name="dispatch")
class VideoDetailView(View):
    template_name = "videos/detail.html"

    def get(self, request, slug):
        # Only browsable videos exist here: drafts and archived videos are 404.
        video = get_object_or_404(services.library_queryset(), slug=slug)
        reason, playback = services.authorize_playback(request.user, video)
        return render(request, self.template_name, {
            "video": video, "thumbnail": services.thumbnail_url(video), "playback": playback,
            "playback_reason": reason, "playback_message": REASON_MESSAGES.get(reason, ""), "nav": "videos",
        })
