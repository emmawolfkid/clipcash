"""
Administrator console (/manage/). Staff only; every section re-checks its
permission server-side, and every change goes through the owning service.
"""

import json
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.advertising import services as advertising
from apps.advertising.models import Campaign, Organization
from apps.audit.admin import can_view_audit
from apps.audit.models import AuditEvent
from apps.core.models import Job
from apps.core.services import can_change_site_configuration
from apps.memberships.models import Subscription
from apps.memberships.services import is_super_admin
from apps.notifications.models import OutboundEmail
from apps.videos import services as videos
from apps.videos.forms import CategoryForm, VideoForm
from apps.videos.models import Video, VideoCategory
from apps.viewing import services as viewing
from apps.viewing.models import WatchSession

console = method_decorator(staff_member_required(login_url="admin:login"), name="dispatch")


def _form_errors(form, error):
    if isinstance(error, ValidationError) and hasattr(error, "error_dict"):
        for field, messages_ in error.message_dict.items():
            form.add_error(field if field in form.fields else None, messages_)
    else:
        form.add_error(None, getattr(error, "messages", [str(error)]))


def console_links(user):
    """Navigation entries; each target enforces its own permission too."""
    can = {
        "members": is_super_admin(user) or user.has_perm("accounts.view_user"),
        "plans": is_super_admin(user),
        "videos": videos.can_use_video_console(user),
        "categories": videos.can_manage_categories(user),
        "settings": can_change_site_configuration(user),
        "audit": can_view_audit(user),
        "advertisers": advertising.can_review_advertisers(user),
        "campaigns": advertising.can_review_campaigns(user),
        "viewing": viewing.can_review_viewing(user),
    }
    return can


@console
class OverviewView(View):
    def get(self, request):
        now = timezone.now()
        User = get_user_model()
        video_counts = dict(Video.objects.values_list("publication_status").annotate(n=Count("id")))
        stats = {
            "members": User.objects.filter(role=User.Role.MEMBER).count(),
            "active_members": User.objects.filter(role=User.Role.MEMBER, status=User.Status.ACTIVE).count(),
            "active_memberships": Subscription.objects.filter(status=Subscription.Status.ACTIVE, starts_at__lte=now,
                                                              expires_at__gt=now).count(),
            "pending_memberships": Subscription.objects.filter(status=Subscription.Status.PENDING).count(),
            "published_videos": video_counts.get(Video.Publication.PUBLISHED, 0),
            "draft_videos": video_counts.get(Video.Publication.DRAFT, 0),
            "processing_videos": Video.objects.filter(processing_status__in=[Video.Processing.UPLOADING,
                                                                             Video.Processing.PROCESSING]).count(),
            "failed_jobs": Job.objects.filter(status=Job.Status.FAILED,
                                              finished_at__gte=now - timedelta(days=1)).count(),
            "pending_emails": OutboundEmail.objects.filter(status=OutboundEmail.Status.PENDING).count(),
            "advertisers_pending": Organization.objects.filter(
                verification_status=Organization.Verification.PENDING).count(),
            "advertisers_approved": Organization.objects.filter(
                verification_status=Organization.Verification.APPROVED).count(),
            "campaigns_submitted": Campaign.objects.filter(status=Campaign.Status.SUBMITTED).count(),
            "campaigns_active": Campaign.objects.filter(status=Campaign.Status.ACTIVE).count(),
            "viewing_review": WatchSession.objects.filter(review_status=WatchSession.Review.PENDING).count(),
            "viewing_verified": WatchSession.objects.filter(outcome=WatchSession.Outcome.VERIFIED_EVIDENCE).count(),
        }
        return render(request, "administration/overview.html",
                      {"nav": "overview", "stats": stats, "can": console_links(request.user), "provider": settings.VIDEO_PROVIDER})


# --- Videos -----------------------------------------------------------------------


def _require_console(user):
    if not videos.can_use_video_console(user):
        raise PermissionDenied


@console
class VideoListView(View):
    def get(self, request):
        _require_console(request.user)
        qs = Video.objects.select_related("category").order_by("-created_at")
        query = request.GET.get("q", "").strip()[:100]
        if query:
            qs = qs.filter(Q(title__icontains=query) | Q(slug__icontains=query))
        filters = {"category": request.GET.get("category", ""), "processing": request.GET.get("processing", ""),
                   "publication": request.GET.get("publication", "")}
        if filters["category"]:
            qs = qs.filter(category__slug=filters["category"])
        if filters["processing"] in Video.Processing.values:
            qs = qs.filter(processing_status=filters["processing"])
        if filters["publication"] in Video.Publication.values:
            qs = qs.filter(publication_status=filters["publication"])
        page = Paginator(qs, 25).get_page(request.GET.get("page"))
        params = request.GET.copy()
        params.pop("page", None)
        return render(request, "administration/videos/list.html", {
            "page": page, "page_query": params.urlencode(), "query": query, "filters": filters, "categories": VideoCategory.objects.all(),
            "processing_choices": Video.Processing.choices, "publication_choices": Video.Publication.choices,
            "can": console_links(request.user), "can_manage": videos.can_manage_videos(request.user), "nav": "videos",
        })


@console
class VideoCreateView(View):
    template_name = "administration/videos/form.html"

    def _demo(self):
        return settings.VIDEO_PROVIDER == "demo"

    def get(self, request):
        if not videos.can_manage_videos(request.user):
            raise PermissionDenied
        return render(request, self.template_name, {"form": VideoForm(demo=self._demo()), "demo": self._demo(),
                                                    "can": console_links(request.user), "nav": "videos"})

    def post(self, request):
        if not videos.can_manage_videos(request.user):
            raise PermissionDenied
        form = VideoForm(request.POST, demo=self._demo())
        if form.is_valid():
            try:
                video = videos.create_video(request.user, **form.video_values())
                messages.success(request, _("Video created."))
                return redirect("administration:video_detail", pk=video.pk)
            except (ValidationError, videos.VideoError) as error:
                _form_errors(form, error)
        return render(request, self.template_name, {"form": form, "demo": self._demo(),
                                                    "can": console_links(request.user), "nav": "videos"})


@console
class VideoDetailView(View):
    template_name = "administration/videos/detail.html"

    def _context(self, request, video, form=None):
        return {
            "video": video, "form": form or VideoForm(instance=video, demo=video.is_demo),
            "thumbnail": videos.thumbnail_url(video), "can": console_links(request.user), "nav": "videos",
            "can_manage": videos.can_manage_videos(request.user),
            "can_publish": videos.can_publish_videos(request.user),
            "can_delete_media": is_super_admin(request.user),
            "upload_allowed": (video.provider == Video.Provider.BUNNY
                               and video.publication_status != Video.Publication.ARCHIVED
                               and video.processing_status in (Video.Processing.PENDING, Video.Processing.UPLOADING,
                                                               Video.Processing.FAILED)),
            "upload_types": ",".join(settings.VIDEO_UPLOAD_CONTENT_TYPES), "upload_max_mb": settings.VIDEO_UPLOAD_MAX_MB,
        }

    def get(self, request, pk):
        _require_console(request.user)
        video = get_object_or_404(Video.objects.select_related("category"), pk=pk)
        return render(request, self.template_name, self._context(request, video))

    def post(self, request, pk):
        if not videos.can_manage_videos(request.user):
            raise PermissionDenied
        video = get_object_or_404(Video, pk=pk)
        form = VideoForm(request.POST, instance=Video.objects.get(pk=pk), demo=video.is_demo)
        if form.is_valid():
            try:
                values = form.video_values()
                values["slug"] = values.get("slug") or video.slug  # Blank keeps the current address.
                videos.update_video(request.user, video, values)
                messages.success(request, _("Changes saved."))
                return redirect("administration:video_detail", pk=pk)
            except (ValidationError, videos.VideoError) as error:
                _form_errors(form, error)
        return render(request, self.template_name, self._context(request, Video.objects.get(pk=pk), form))


ACTIONS = {
    "publish": (videos.publish, _("Video published.")),
    "unpublish": (videos.unpublish, _("Video unpublished (back to draft).")),
    "archive": (videos.archive, _("Video archived.")),
    "delete-media": (videos.request_media_deletion, _("Media deletion requested.")),
}
# Permanent actions need an explicit confirmation field in the POST.
CONFIRM_ACTIONS = {"archive", "delete-media"}


@console
class VideoActionView(View):
    def post(self, request, pk, action):
        video = get_object_or_404(Video, pk=pk)
        if action == "sync":
            if not videos.can_manage_videos(request.user):
                raise PermissionDenied
            if video.provider == Video.Provider.BUNNY and video.provider_video_id:
                videos.enqueue_sync(video)
                messages.success(request, _("Status check queued. Refresh in a minute."))
            return redirect("administration:video_detail", pk=pk)
        if action not in ACTIONS:
            raise PermissionDenied
        operation, success = ACTIONS[action]
        if action in CONFIRM_ACTIONS and request.POST.get("confirm") != "yes":
            messages.error(request, _("Tick the confirmation box first. This action cannot be undone."))
            return redirect("administration:video_detail", pk=pk)
        try:
            operation(request.user, video)
            messages.success(request, success)
        except videos.NotAuthorized:
            raise PermissionDenied
        except videos.VideoError as error:
            messages.error(request, str(error))
        return redirect("administration:video_detail", pk=pk)


@console
class UploadView(View):
    """JSON endpoints used by static/js/video-upload.js (same-origin, CSRF-protected)."""

    def post(self, request, pk, step):
        video = get_object_or_404(Video, pk=pk)
        if not videos.can_manage_videos(request.user):
            raise PermissionDenied
        try:
            if step == "authorize":
                data = json.loads(request.body or b"{}")
                auth = videos.prepare_upload(request.user, video, filename=str(data.get("name", ""))[:200],
                                             size=data.get("size"), content_type=str(data.get("type", "")))
                return JsonResponse({"endpoint": auth.endpoint, "headers": auth.headers, "metadata": auth.metadata,
                                     "expiresAt": auth.expires_at.isoformat() if auth.expires_at else None})
            if step == "finished":
                videos.report_upload_finished(request.user, video)
                return JsonResponse({"status": "processing"})
            if step == "failed":
                data = json.loads(request.body or b"{}")
                videos.report_upload_failed(request.user, video, str(data.get("reason", "unknown")))
                return JsonResponse({"status": "failed"})
        except ValueError:
            return JsonResponse({"error": "Invalid request."}, status=400)
        except videos.NotAuthorized:
            raise PermissionDenied
        except videos.VideoError as error:
            return JsonResponse({"error": str(error), "code": error.code}, status=400)
        return JsonResponse({"error": "Unknown step."}, status=404)


# --- Categories ---------------------------------------------------------------------


@console
class CategoryListView(View):
    template_name = "administration/categories/list.html"

    def _render(self, request, form):
        return render(request, self.template_name, {
            "categories": VideoCategory.objects.annotate(video_count=Count("videos")).order_by("display_order", "name"),
            "form": form, "can": console_links(request.user), "nav": "categories"})

    def get(self, request):
        if not videos.can_manage_categories(request.user):
            raise PermissionDenied
        return self._render(request, CategoryForm())

    def post(self, request):
        if not videos.can_manage_categories(request.user):
            raise PermissionDenied
        form = CategoryForm(request.POST)
        if form.is_valid():
            try:
                videos.create_category(request.user, **form.category_values())
                messages.success(request, _("Category created."))
                return redirect("administration:categories")
            except ValidationError as error:
                _form_errors(form, error)
        return self._render(request, form)


@console
class CategoryEditView(View):
    template_name = "administration/categories/form.html"

    def get(self, request, pk):
        if not videos.can_manage_categories(request.user):
            raise PermissionDenied
        category = get_object_or_404(VideoCategory, pk=pk)
        return render(request, self.template_name, {"form": CategoryForm(instance=category), "category": category,
                                                    "can": console_links(request.user), "nav": "categories"})

    def post(self, request, pk):
        if not videos.can_manage_categories(request.user):
            raise PermissionDenied
        category = get_object_or_404(VideoCategory, pk=pk)
        if "toggle" in request.POST:
            videos.set_category_active(request.user, category, not category.is_active)
            messages.success(request, _("Category updated."))
            return redirect("administration:categories")
        form = CategoryForm(request.POST, instance=VideoCategory.objects.get(pk=pk))
        if form.is_valid():
            try:
                values = form.category_values()
                values["slug"] = values.get("slug") or category.slug
                videos.update_category(request.user, category, values)
                messages.success(request, _("Category saved."))
                return redirect("administration:categories")
            except ValidationError as error:
                _form_errors(form, error)
        return render(request, self.template_name, {"form": form, "category": category,
                                                    "can": console_links(request.user), "nav": "categories"})


# --- Advertisers and campaigns (Phase 05) ------------------------------------------------


def _reason_text(request):
    return request.POST.get("reason", "")[:300]


def _error_text(error):
    return " ".join(getattr(error, "messages", [str(error)]))


@console
class AdvertiserQueueView(View):
    def get(self, request):
        if not advertising.can_review_advertisers(request.user):
            raise PermissionDenied
        status = request.GET.get("status", Organization.Verification.PENDING)
        qs = Organization.objects.annotate(campaign_count=Count("campaigns")).order_by("submitted_at", "display_name")
        if status in Organization.Verification.values:
            qs = qs.filter(verification_status=status)
        query = request.GET.get("q", "").strip()[:100]
        if query:
            qs = qs.filter(Q(display_name__icontains=query) | Q(legal_name__icontains=query))
        page = Paginator(qs, 25).get_page(request.GET.get("page"))
        params = request.GET.copy()
        params.pop("page", None)
        return render(request, "administration/advertisers/list.html", {
            "page": page, "status": status, "query": query, "statuses": Organization.Verification.choices,
            "page_query": params.urlencode(), "can": console_links(request.user), "nav": "advertisers"})


@console
class AdvertiserReviewView(View):
    template_name = "administration/advertisers/detail.html"

    def get(self, request, pk):
        if not advertising.can_use_advertising_console(request.user):
            raise PermissionDenied
        organization = get_object_or_404(Organization, pk=pk)
        can_review = advertising.can_review_advertisers(request.user)
        grants = organization.video_grants.select_related("video").filter(revoked_at__isnull=True)
        return render(request, self.template_name, {
            "organization": organization, "can_review": can_review,
            "can_grant": advertising.can_manage_advertiser_videos(request.user),
            # Contact and identity details only for advertiser reviewers.
            "members": organization.memberships.select_related("user").filter(is_active=True) if can_review else [],
            "campaigns": organization.campaigns.order_by("-created_at")[:20],
            "report": advertising.organization_report(organization),
            "grants": grants,
            "grantable": (Video.objects.exclude(publication_status=Video.Publication.ARCHIVED)
                          .exclude(pk__in=grants.values("video_id")).order_by("title")[:200]),
            "can": console_links(request.user), "nav": "advertisers"})

    def post(self, request, pk):
        organization = get_object_or_404(Organization, pk=pk)
        action = request.POST.get("action", "")
        try:
            if action in advertising.ORG_DECISIONS:
                advertising.review_organization(request.user, organization, action,
                                                expected_status=request.POST.get("expected_status", ""),
                                                reason=_reason_text(request))
                messages.success(request, _("Decision saved."))
            elif action == "grant":
                video = get_object_or_404(Video, pk=request.POST.get("video"))
                advertising.grant_video(request.user, organization, video)
                messages.success(request, _("Video authorized for this advertiser."))
            elif action == "revoke":
                advertising.revoke_video_grant(request.user, organization, request.POST.get("grant"))
                messages.success(request, _("Video authorization revoked."))
            else:
                raise PermissionDenied
        except advertising.NotAuthorized:
            raise PermissionDenied from None
        except (ValidationError, advertising.AdvertisingError) as error:
            messages.error(request, _error_text(error))
        return redirect("administration:advertiser_review", pk=pk)


@console
class CampaignQueueView(View):
    def get(self, request):
        if not advertising.can_review_campaigns(request.user):
            raise PermissionDenied
        status = request.GET.get("status", Campaign.Status.SUBMITTED)
        qs = Campaign.objects.select_related("organization", "video").order_by("submitted_at", "-created_at")
        if status in Campaign.Status.values:
            qs = qs.filter(status=status)
        query = request.GET.get("q", "").strip()[:100]
        if query:
            qs = qs.filter(Q(name__icontains=query) | Q(organization__display_name__icontains=query))
        page = Paginator(qs, 25).get_page(request.GET.get("page"))
        params = request.GET.copy()
        params.pop("page", None)
        return render(request, "administration/campaigns/list.html", {
            "page": page, "status": status, "query": query, "statuses": Campaign.Status.choices,
            "page_query": params.urlencode(), "can": console_links(request.user), "nav": "campaigns"})


STAFF_CAMPAIGN_ACTIONS = {"approve", "reject", "pause", "resume", "cancel", "archive"}


@console
class CampaignReviewView(View):
    def get(self, request, pk):
        if not advertising.can_review_campaigns(request.user):
            raise PermissionDenied
        campaign = get_object_or_404(Campaign.objects.select_related("organization", "video", "video__category"),
                                     pk=pk)
        preview_reason, preview = advertising.preview_playback(request.user, campaign)
        eligibility = advertising.campaign_eligibility(campaign)
        return render(request, "administration/campaigns/detail.html", {
            "campaign": campaign, "plans": campaign.target_plans.order_by("name"),
            "eligibility": eligibility, "eligibility_message": advertising.eligibility_message(eligibility.reason),
            "video_authorized": advertising.video_authorized(campaign.organization, campaign.video),
            "video_available": advertising.video_available(campaign.video),
            "preview": preview, "preview_reason": preview_reason,
            "can": console_links(request.user), "nav": "campaigns"})

    def post(self, request, pk):
        campaign = get_object_or_404(Campaign, pk=pk)
        action = request.POST.get("action", "")
        if action not in STAFF_CAMPAIGN_ACTIONS:
            raise PermissionDenied
        try:
            expected = int(request.POST.get("expected_revision", "-1"))
        except ValueError:
            expected = -1
        try:
            if action == "approve":
                advertising.approve_campaign(request.user, campaign, expected_revision=expected,
                                             notes=_reason_text(request))
            elif action == "reject":
                advertising.reject_campaign(request.user, campaign, expected_revision=expected,
                                            reason=_reason_text(request))
            elif action == "pause":
                advertising.pause_campaign(request.user, campaign, reason=_reason_text(request))
            elif action == "cancel":
                if request.POST.get("confirm") != "yes":
                    raise advertising.InvalidState("Tick the confirmation box first. This cannot be undone.")
                advertising.cancel_campaign(request.user, campaign, reason=_reason_text(request))
            elif action == "resume":
                advertising.resume_campaign(request.user, campaign)
            else:
                advertising.archive_campaign(request.user, campaign)
            messages.success(request, _("Campaign updated."))
        except advertising.NotAuthorized:
            raise PermissionDenied from None
        except (ValidationError, advertising.AdvertisingError) as error:
            messages.error(request, _error_text(error))
        return redirect("administration:campaign_review", pk=pk)


# --- Viewing review (Phase 06) -------------------------------------------------------------


@console
class ViewingListView(View):
    def get(self, request):
        if not viewing.can_review_viewing(request.user):
            raise PermissionDenied
        qs = WatchSession.objects.select_related("member", "campaign").order_by("-created_at")
        filters = {key: request.GET.get(key, "").strip()[:100] for key in ("member", "campaign", "outcome", "state",
                                                                           "flag", "review")}
        if filters["member"]:
            qs = qs.filter(member__email__icontains=filters["member"])
        if filters["campaign"]:
            qs = qs.filter(campaign__name__icontains=filters["campaign"])
        if filters["outcome"] in WatchSession.Outcome.values:
            qs = qs.filter(outcome=filters["outcome"])
        if filters["state"] in WatchSession.State.values:
            qs = qs.filter(state=filters["state"])
        if filters["flag"] in VIEWING_FLAGS:
            qs = qs.filter(flags__contains=[filters["flag"]])
        if filters["review"]:
            qs = qs.filter(review_status=WatchSession.Review.PENDING)
        page = Paginator(qs, 25).get_page(request.GET.get("page"))
        params = request.GET.copy()
        params.pop("page", None)
        patterns = (WatchSession.objects.filter(finalized_at__gte=timezone.now() - timedelta(days=30),
                                                outcome__in=[WatchSession.Outcome.REJECTED,
                                                             WatchSession.Outcome.SUSPICIOUS_REVIEW])
                    .values("member__email").annotate(n=Count("id")).filter(n__gte=2).order_by("-n")[:10])
        return render(request, "administration/viewing/list.html", {
            "page": page, "filters": filters, "page_query": params.urlencode(), "patterns": patterns,
            "outcomes": WatchSession.Outcome.choices, "states": WatchSession.State.choices,
            "flag_choices": VIEWING_FLAGS, "can": console_links(request.user), "nav": "viewing"})


@console
class ViewingReviewView(View):
    def get(self, request, pk):
        if not viewing.can_review_viewing(request.user):
            raise PermissionDenied
        session = get_object_or_404(WatchSession.objects.select_related("member", "campaign"), pk=pk)
        history = AuditEvent.objects.filter(target_model="watchsession", target_id=str(session.pk)).order_by(
            "occurred_at")[:100]
        return render(request, "administration/viewing/detail.html", {
            "session": session, "history": history, "can": console_links(request.user), "nav": "viewing"})

    def post(self, request, pk):
        session = get_object_or_404(WatchSession, pk=pk)
        try:
            viewing.review_session(request.user, session, request.POST.get("decision", ""),
                                   expected_status=request.POST.get("expected_status", ""),
                                   reason=request.POST.get("reason", "")[:300])
            messages.success(request, _("Review decision saved."))
        except viewing.Denied:
            raise PermissionDenied from None
        except (ValidationError, viewing.ViewingError) as error:
            messages.error(request, _error_text(error))
        return redirect("administration:viewing_review", pk=pk)


VIEWING_FLAGS = ["excessive_events", "heartbeat_gaps", "impossible_speed", "invalid_evidence", "large_seeks",
                 "rapid_attempts", "replayed_events"]
