"""
Administrator console (/manage/). Staff only; every section re-checks its
permission server-side, and every change goes through the owning service.
"""

import json
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.advertising import services as advertising
from apps.advertising.models import Campaign, Organization
from apps.audit.admin import can_view_audit
from apps.wallets import wallet_services
from apps.wallets.models import Wallet, WalletDiscrepancy, WalletRestriction
from apps.payments import services as payments
from apps.payments import simulator as payment_simulator
from apps.payments.models import (
    MembershipActivationPreview,
    PaymentDispute,
    PaymentIntent,
    ReconciliationException,
    RefundRequest,
    SandboxAdvertiserFunds,
    SandboxCampaignAllocation,
)
from apps.payments.providers import sandbox_active as payments_sandbox_active
from apps.rewards import services as rewards
from apps.rewards.models import DemoReward, RewardPolicy
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
        "rewards": rewards.can_use_rewards_console(user),
        "payments": payments.can_view_payments(user) and payments_sandbox_active(),
        "wallets": wallet_services.can_view_wallets(user),
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
            "demo_rewards": DemoReward.objects.filter(status=DemoReward.Status.QUALIFIED).count(),
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
            if request.POST.get("decision") == "revoke":
                viewing.revoke_verified_session(request.user, session, reason=request.POST.get("reason", "")[:300])
                messages.success(request, _("Verified view revoked; its demo reward (if any) was reversed."))
                return redirect("administration:viewing_review", pk=pk)
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


# --- Demo rewards (Phase 07) ----------------------------------------------------------------


@console
class RewardPolicyListView(View):
    def get(self, request):
        return self._render(request)

    def _render(self, request):
        if not rewards.can_use_rewards_console(request.user):
            raise PermissionDenied
        page = Paginator(RewardPolicy.objects.select_related("campaign").order_by("campaign__name", "-version"),
                         25).get_page(request.GET.get("page"))
        campaigns = (Campaign.objects.select_related("organization")
                     .filter(status__in=rewards.POLICY_CAMPAIGN_STATES).order_by("name"))
        return render(request, "administration/rewards/policies.html", {
            "policies": page.object_list, "policies_page": page, "page": page, "campaigns": campaigns,
            "can_manage": rewards.can_manage_policies(request.user), "can": console_links(request.user),
            "nav": "rewards"})

    def post(self, request):
        campaign = get_object_or_404(Campaign, pk=request.POST.get("campaign"))
        try:
            policy = rewards.create_policy(request.user, campaign, reward_amount=_whole_number(request, "reward_amount"),
                                           notes=request.POST.get("notes", ""))
            messages.success(request, _("Policy draft saved. Another administrator must approve it."))
            return redirect("administration:reward_policy", pk=policy.pk)
        except rewards.NotAuthorized:
            raise PermissionDenied from None
        except (ValidationError, rewards.RewardsError) as error:
            messages.error(request, _error_text(error))
        return redirect("administration:reward_policies")


def _whole_number(request, name):
    raw = request.POST.get(name, "").strip()
    if not raw.isdigit():
        raise ValidationError({name: "Enter a whole number of shillings."})
    return int(raw)


@console
class RewardPolicyView(View):
    def get(self, request, pk):
        if not rewards.can_use_rewards_console(request.user):
            raise PermissionDenied
        policy = get_object_or_404(RewardPolicy.objects.select_related("campaign", "created_by", "decided_by"), pk=pk)
        return render(request, "administration/rewards/policy.html", {
            "policy": policy, "history": RewardPolicy.objects.filter(campaign=policy.campaign).order_by("-version"),
            "reward_count": policy.demo_rewards.count(), "can_approve": rewards.can_approve_policies(request.user),
            "can": console_links(request.user), "nav": "rewards"})

    def post(self, request, pk):
        policy = get_object_or_404(RewardPolicy, pk=pk)
        action = request.POST.get("action", "")
        expected = request.POST.get("expected_status", "")
        reason = request.POST.get("reason", "")[:300]
        try:
            if action == "approve":
                rewards.approve_policy(request.user, policy, expected_status=expected)
            elif action == "reject":
                rewards.reject_policy(request.user, policy, reason=reason, expected_status=expected)
            elif action == "retire":
                rewards.retire_policy(request.user, policy, reason=reason)
            else:
                raise PermissionDenied
            messages.success(request, _("Policy updated."))
        except rewards.NotAuthorized:
            raise PermissionDenied from None
        except (ValidationError, rewards.RewardsError) as error:
            messages.error(request, _error_text(error))
        return redirect("administration:reward_policy", pk=pk)


@console
class DemoRewardListView(View):
    def get(self, request):
        if not rewards.can_use_rewards_console(request.user):
            raise PermissionDenied
        qs = DemoReward.objects.select_related("member", "campaign").order_by("-decided_at")
        filters = {key: request.GET.get(key, "").strip()[:100] for key in ("member", "status", "campaign")}
        if filters["member"]:
            qs = qs.filter(member__email__icontains=filters["member"])
        if filters["status"] in DemoReward.Status.values:
            qs = qs.filter(status=filters["status"])
        campaign_filter = None
        if filters["campaign"]:
            try:
                campaign_filter = uuid.UUID(filters["campaign"])
                qs = qs.filter(campaign_id=campaign_filter)
            except ValueError:
                filters["campaign"] = ""
        page = Paginator(qs, 25).get_page(request.GET.get("page"))
        params = request.GET.copy()
        params.pop("page", None)
        reason_counts = {}
        for codes in DemoReward.objects.filter(status=DemoReward.Status.REJECTED).values_list("reasons", flat=True)[:2000]:
            for code in codes:
                reason_counts[code] = reason_counts.get(code, 0) + 1
        return render(request, "administration/rewards/rewards.html", {
            "page": page, "filters": filters, "page_query": params.urlencode(),
            "statuses": DemoReward.Status.choices, "totals": rewards.campaign_totals(),
            "reasons": [{"code": k, "n": v} for k, v in sorted(reason_counts.items())],
            "pending_count": rewards.pending_sessions(10_000).count(),
            "can_manage": rewards.can_manage_rewards(request.user), "can": console_links(request.user),
            "nav": "rewards"})

    def post(self, request):
        action = request.POST.get("action", "")
        try:
            if action == "evaluate":
                if not rewards.can_manage_rewards(request.user):
                    raise PermissionDenied
                counts = rewards.evaluate_pending()
                messages.success(request, _("Evaluated: %(q)s recorded, %(r)s not eligible, %(w)s waiting.")
                                 % {"q": counts["qualified"], "r": counts["rejected"], "w": counts["waiting"]})
            elif action == "reverse":
                reward = get_object_or_404(DemoReward, pk=request.POST.get("reward"))
                rewards.reverse_reward(request.user, reward, reason=request.POST.get("reason", "")[:300])
                messages.success(request, _("Demo reward reversed."))
            else:
                raise PermissionDenied
        except rewards.NotAuthorized:
            raise PermissionDenied from None
        except (ValidationError, rewards.RewardsError) as error:
            messages.error(request, _error_text(error))
        return redirect("administration:demo_rewards")


# --- Payments, SANDBOX (Phase 08) ---------------------------------------------------------


SIMULATOR_ACTIONS = [("pay", _("Pay (correct amount)")), ("pay_wrong_amount", _("Pay wrong amount")),
                     ("fail", _("Fail")), ("settle", _("Settle")), ("chargeback", _("Chargeback")),
                     ("replay", _("Replay last notice")), ("orphan", _("Unknown-reference notice")),
                     ("forged", _("Forged notice"))]


def _require_payments_console(user):
    if not payments.can_view_payments(user) or not payments_sandbox_active():
        raise PermissionDenied


@console
class PaymentListView(View):
    def get(self, request):
        _require_payments_console(request.user)
        qs = PaymentIntent.objects.select_related("payer", "organization").order_by("-created_at")
        filters = {key: request.GET.get(key, "").strip()[:40] for key in ("q", "status", "purpose")}
        if filters["q"]:
            qs = qs.filter(Q(reference__icontains=filters["q"]) | Q(provider_reference__icontains=filters["q"]))
        if filters["status"] in PaymentIntent.Status.values:
            qs = qs.filter(status=filters["status"])
        if filters["purpose"] in PaymentIntent.Purpose.values:
            qs = qs.filter(purpose=filters["purpose"])
        page = Paginator(qs, 25).get_page(request.GET.get("page"))
        params = request.GET.copy()
        params.pop("page", None)
        return render(request, "administration/payments/list.html", {
            "page": page, "filters": filters, "page_query": params.urlencode(),
            "statuses": PaymentIntent.Status.choices, "purposes": PaymentIntent.Purpose.choices,
            "open_exceptions": ReconciliationException.objects.filter(status="OPEN").count(),
            "can": console_links(request.user), "nav": "payments"})


@console
class PaymentDetailView(View):
    def get(self, request, pk):
        _require_payments_console(request.user)
        intent = get_object_or_404(PaymentIntent.objects.select_related("payer", "organization"), pk=pk)
        return render(request, "administration/payments/detail.html", {
            "intent": intent, "preview": MembershipActivationPreview.objects.filter(intent=intent).first(),
            "events": intent.provider_events.order_by("received_at"), "postings": intent.sandbox_postings.all(),
            "exceptions": intent.exceptions.all(), "refunds": intent.refunds.select_related("requested_by"),
            "disputes": intent.disputes.all(),
            "history": AuditEvent.objects.filter(target_model="paymentintent", target_id=str(intent.pk))
                                         .order_by("occurred_at")[:100],
            "can_simulate": payments.can_operate_sandbox(request.user),
            "can_refund": payments.can_approve_refunds(request.user), "simulator_actions": SIMULATOR_ACTIONS,
            "can": console_links(request.user), "nav": "payments"})

    def post(self, request, pk):
        _require_payments_console(request.user)
        intent = get_object_or_404(PaymentIntent, pk=pk)
        action = request.POST.get("action", "")
        try:
            if action == "simulate":
                status, outcome = payment_simulator.simulate(request.user, intent, request.POST.get("simulate", ""))
                messages.info(request, _("Simulated provider notice: HTTP %(s)s, %(o)s.") % {"s": status, "o": outcome})
            elif action == "request_refund":
                payments.request_refund(request.user, intent, reason=request.POST.get("reason", ""))
                messages.success(request, _("Refund requested. A second person must approve it."))
            elif action == "decide_refund":
                refund = get_object_or_404(RefundRequest, pk=request.POST.get("refund"), intent=intent)
                payments.decide_refund(request.user, refund, approve=request.POST.get("approve") == "1",
                                       note=request.POST.get("note", ""))
                messages.success(request, _("Refund decision saved."))
            elif action == "resolve_dispute":
                dispute = get_object_or_404(PaymentDispute, pk=request.POST.get("dispute"), intent=intent)
                payments.resolve_dispute(request.user, dispute, won=request.POST.get("won") == "1",
                                         note=request.POST.get("note", ""))
                messages.success(request, _("Dispute resolved."))
            else:
                raise PermissionDenied
        except payments.NotAuthorized:
            raise PermissionDenied from None
        except (ValidationError, payments.PaymentError) as error:
            messages.error(request, _error_text(error))
        return redirect("administration:payment", pk=pk)


@console
class PaymentExceptionsView(View):
    def get(self, request):
        _require_payments_console(request.user)
        qs = ReconciliationException.objects.select_related("intent", "organization").order_by("status", "-created_at")
        page = Paginator(qs, 25).get_page(request.GET.get("page"))
        return render(request, "administration/payments/exceptions.html", {
            "page": page, "can_review": payments.can_review_reconciliation(request.user),
            "can": console_links(request.user), "nav": "payments"})

    def post(self, request):
        _require_payments_console(request.user)
        exception = get_object_or_404(ReconciliationException, pk=request.POST.get("exception"))
        try:
            payments.resolve_exception(request.user, exception, action=request.POST.get("action", ""),
                                       note=request.POST.get("note", ""))
            messages.success(request, _("Exception updated."))
        except payments.NotAuthorized:
            raise PermissionDenied from None
        except (ValidationError, payments.PaymentError) as error:
            messages.error(request, _error_text(error))
        return redirect("administration:payment_exceptions")


@console
class PaymentFundingView(View):
    def get(self, request):
        _require_payments_console(request.user)
        balances = [{"account": a, "scope": s, "balance": b} for (a, s), b in payments.sandbox_balances().items()]
        return render(request, "administration/payments/funding.html", {
            "funds": SandboxAdvertiserFunds.objects.select_related("organization").order_by("organization__display_name"),
            "allocations": SandboxCampaignAllocation.objects.select_related("campaign", "organization")
                                                            .order_by("-created_at")[:100],
            "balances": balances, "balance_total": sum(row["balance"] for row in balances),
            "can": console_links(request.user), "nav": "payments"})


# --- Member wallets (Phase 09) ----------------------------------------------------------------


@console
class WalletListView(View):
    def get(self, request):
        if not wallet_services.can_view_wallets(request.user):
            raise PermissionDenied
        query = request.GET.get("q", "").strip()[:100]
        page = Paginator(wallet_services.directory(query), 25).get_page(request.GET.get("page"))
        params = request.GET.copy()
        params.pop("page", None)
        return render(request, "administration/wallets/list.html", {
            "page": page, "query": query, "page_query": params.urlencode(),
            "open_discrepancies": WalletDiscrepancy.objects.filter(status="OPEN").count(),
            "can": console_links(request.user), "nav": "wallets"})


@console
class WalletDetailView(View):
    def get(self, request, pk):
        try:
            wallet = wallet_services.wallet_for_staff(request.user, pk)
        except wallet_services.NotAuthorized:
            raise PermissionDenied from None
        except wallet_services.NotEligible:
            raise Http404 from None
        return render(request, "administration/wallets/detail.html", {
            "wallet": wallet, "balances": wallet_services.balances(wallet),
            "restrictions": wallet.restrictions.select_related("placed_by", "lift_requested_by"),
            "transactions": list(wallet_services.history(wallet)[:50]),
            "discrepancies": wallet.discrepancies.order_by("-created_at")[:20],
            "history": AuditEvent.objects.filter(target_model="wallet", target_id=str(wallet.pk))
                                         .exclude(action="wallets.wallet.inspected").order_by("occurred_at")[:100],
            "kinds": WalletRestriction.Kind.choices,
            "can_manage": wallet_services.can_manage_restrictions(request.user),
            "can": console_links(request.user), "nav": "wallets"})

    def post(self, request, pk):
        if not wallet_services.can_view_wallets(request.user):
            raise PermissionDenied
        wallet = get_object_or_404(Wallet, pk=pk)
        action = request.POST.get("action", "")
        try:
            if action == "restrict":
                wallet_services.place_restriction(request.user, wallet, kind=request.POST.get("kind", ""),
                                                  reason=request.POST.get("reason", ""))
            elif action in ("request_lift", "decide_lift"):
                restriction = get_object_or_404(WalletRestriction, pk=request.POST.get("restriction"), wallet=wallet)
                if action == "request_lift":
                    wallet_services.request_lift(request.user, restriction, reason=request.POST.get("reason", ""))
                else:
                    wallet_services.decide_lift(request.user, restriction, approve=request.POST.get("approve") == "1",
                                                note=request.POST.get("note", ""))
            else:
                raise PermissionDenied
            messages.success(request, _("Wallet updated."))
        except wallet_services.NotAuthorized:
            raise PermissionDenied from None
        except (ValidationError, wallet_services.WalletError) as error:
            messages.error(request, _error_text(error))
        return redirect("administration:wallet", pk=pk)


@console
class WalletReconciliationView(View):
    def get(self, request):
        if not wallet_services.can_view_wallets(request.user):
            raise PermissionDenied
        qs = WalletDiscrepancy.objects.select_related("wallet", "account").order_by("status", "-created_at")
        return render(request, "administration/wallets/reconciliation.html", {
            "page": Paginator(qs, 25).get_page(request.GET.get("page")),
            "can_manage": wallet_services.can_manage_restrictions(request.user),
            "can": console_links(request.user), "nav": "wallets"})

    def post(self, request):
        if not wallet_services.can_view_wallets(request.user):
            raise PermissionDenied
        try:
            if request.POST.get("action") == "run":
                summary = wallet_services.reconcile()
                messages.success(request, _("Checked %(w)s wallets: %(f)s findings.")
                                 % {"w": summary["wallets"], "f": summary["findings"]})
            elif request.POST.get("action") == "resolve":
                discrepancy = get_object_or_404(WalletDiscrepancy, pk=request.POST.get("discrepancy"))
                wallet_services.resolve_discrepancy(request.user, discrepancy, note=request.POST.get("note", ""))
                messages.success(request, _("Finding resolved."))
            else:
                raise PermissionDenied
        except wallet_services.NotAuthorized:
            raise PermissionDenied from None
        except (ValidationError, wallet_services.WalletError) as error:
            messages.error(request, _error_text(error))
        return redirect("administration:wallet_reconciliation")
