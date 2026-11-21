from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
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

# PAY-LIVE-01B: why the catalogue cannot be browsed (shown after the redirect).
CATALOGUE_MESSAGES = {
    "not_authenticated": _("Sign in to browse videos. Videos are available to members with an active membership."),
    "account_inactive": _("Your account cannot use videos at the moment. Please contact support."),
    "email_unverified": _("Confirm your email address and choose a membership to browse videos."),
    "no_membership": _("Videos are available to members with an active membership. Choose a plan to get "
                       "started."),
    "membership_suspended": _("Your membership is suspended, so videos are unavailable. Please contact support."),
    "entitlement_missing": _("Your membership plan does not include this video. Compare plans to find one that "
                             "does."),
}


def _with_thumbnails(videos):
    for video in videos:
        video.thumbnail = services.thumbnail_url(video)
    return videos


def _catalogue_refusal(request, reason):
    """Leave the catalogue with a clear reason. Anonymous visitors go to sign in (and come
    back afterwards); everyone else goes to the membership plans, which are never behind
    this check, so no redirect loop is possible."""
    if reason == "feature_disabled":
        raise Http404  # Memberships are off: there is no membership catalogue to offer.
    messages.info(request, CATALOGUE_MESSAGES.get(reason, CATALOGUE_MESSAGES["no_membership"]))
    if reason == "not_authenticated":
        return redirect_to_login(request.get_full_path())
    return redirect("memberships:plans")


@method_decorator(feature_required("videos"), name="dispatch")
class LibraryView(View):
    """Member video library (PAY-LIVE-01B): only members with a current, non-suspended
    membership may browse, and only the videos their plan's entitlements include."""

    template_name = "videos/library.html"

    def get(self, request, slug=None):
        # Checked before any category or video lookup, so nothing about the catalogue leaks.
        access = services.catalogue_access(request.user)
        if not access:
            return _catalogue_refusal(request, access.reason)
        categories = list(VideoCategory.objects.filter(is_active=True).order_by("display_order", "name"))
        category = get_object_or_404(VideoCategory, slug=slug, is_active=True) if slug else None
        videos = services.member_catalogue(access.subscription)
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
        # The library's rule, applied before the slug is looked up.
        access = services.catalogue_access(request.user)
        if not access:
            return _catalogue_refusal(request, access.reason)
        # Only browsable videos exist here: drafts and archived videos are 404.
        video = get_object_or_404(services.library_queryset(), slug=slug)
        if video.required_entitlement not in (access.subscription.entitlements or []):
            return _catalogue_refusal(request, "entitlement_missing")
        # Playback keeps its own authoritative check; signed player URLs are issued only there.
        reason, playback = services.authorize_playback(request.user, video)
        return render(request, self.template_name, {
            "video": video, "thumbnail": services.thumbnail_url(video), "playback": playback,
            "playback_reason": reason, "playback_message": REASON_MESSAGES.get(reason, ""), "nav": "videos",
        })
