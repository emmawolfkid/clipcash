"""
Member viewing pages (/watch/). Sessions are looked up only among the member's
own sessions (another member's id is "not found"). Evidence endpoints are
same-origin JSON POSTs protected by CSRF; the browser never decides outcomes.
"""

import json

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.views import View

from apps.accounts.access import VerifiedAccountRequiredMixin
from apps.advertising.models import Campaign
from apps.core.features import feature_required

from . import services
from .models import WatchSession

MAX_EVENT_BYTES = 1024
viewing_feature = method_decorator(feature_required("advertisers"), name="dispatch")


def summary(session):
    return {
        "state": session.state, "outcome": session.outcome, "open": session.is_open,
        "progress": session.progress_percent, "credited_seconds": session.credited_ms // 1000,
        "required_seconds": (session.required_ms or 0) // 1000,
    }


@viewing_feature
class WatchListView(VerifiedAccountRequiredMixin, View):
    def get(self, request):
        return render(request, "viewing/list.html", {
            "nav": "watch", "rows": services.available_campaigns(request.user)})


@viewing_feature
class CampaignWatchView(VerifiedAccountRequiredMixin, View):
    template_name = "viewing/campaign.html"

    def get(self, request, campaign_id):
        campaign = get_object_or_404(Campaign.objects.select_related("organization", "video"), pk=campaign_id,
                                     status=Campaign.Status.ACTIVE)
        return render(request, self.template_name, {"nav": "watch", "campaign": campaign})

    def post(self, request, campaign_id):
        try:
            session = services.start_session(request.user, campaign_id)
        except services.NotFound:
            raise Http404 from None
        except services.Denied as error:
            messages.error(request, str(error))
            return redirect("viewing:list")
        return redirect("viewing:session", session_id=session.pk)


@viewing_feature
class SessionView(VerifiedAccountRequiredMixin, View):
    def get(self, request, session_id):
        try:
            session, reason, playback = services.playback_for(request.user, session_id)
        except services.NotFound:
            raise Http404 from None
        return render(request, "viewing/session.html", {
            "nav": "watch", "session": session, "playback": playback, "reason": reason,
            "message": services.denial_message(reason) if reason not in ("ok", "session_closed") else "",
            "heartbeat_seconds": settings.VIEWING_HEARTBEAT_SECONDS})


@viewing_feature
class SessionEventView(VerifiedAccountRequiredMixin, View):
    def post(self, request, session_id):
        if len(request.body) > MAX_EVENT_BYTES:
            return JsonResponse({"accepted": False, "code": "payload_too_large"}, status=413)
        try:
            payload = json.loads(request.body or b"null")
        except ValueError:
            payload = None
        try:
            result = services.record_event(request.user, session_id, payload)
        except services.NotFound:
            return JsonResponse({"accepted": False, "code": "not_found"}, status=404)
        return JsonResponse({"accepted": result.accepted, "code": result.code, **summary(result.session)},
                            status=result.http_status)


@viewing_feature
class SessionFinishView(VerifiedAccountRequiredMixin, View):
    def post(self, request, session_id):
        try:
            session = services.finish_session(request.user, session_id)
        except services.NotFound:
            raise Http404 from None
        if request.headers.get("Accept", "").startswith("application/json"):
            return JsonResponse(summary(session))
        return redirect("viewing:session", session_id=session.pk)


@viewing_feature
class HistoryView(VerifiedAccountRequiredMixin, View):
    def get(self, request):
        page = Paginator(services.member_history(request.user), settings.VIEWING_HISTORY_PAGE_SIZE).get_page(
            request.GET.get("page"))
        return render(request, "viewing/history.html", {"nav": "watch", "page": page,
                                                        "outcomes": WatchSession.Outcome})
