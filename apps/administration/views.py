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
from django.utils.formats import date_format
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.accounts.lookup import identifier_q
from apps.administration import summary as admin_summary
from apps.advertising import services as advertising
from apps.advertising.models import Campaign, Organization
from apps.audit.admin import can_view_audit
from apps.withdrawals import services as withdrawals
from apps.withdrawals.models import DemoWithdrawal, PayoutBeneficiary, PayoutEvent, WithdrawalException
from apps.wallets import wallet_services
from apps.wallets.models import Wallet, WalletDiscrepancy, WalletRestriction
from apps.payments import manual as manual_payments
from apps.payments import services as payments
from apps.payments import simulator as payment_simulator
from apps.payments.models import (
    ManualMembershipPayment,
    MembershipActivationPreview,
    PaymentDispute,
    PaymentIntent,
    ReconciliationException,
    RefundRequest,
    SandboxAdvertiserFunds,
    SandboxCampaignAllocation,
)
from apps.payments.providers import PaymentsDisabled
from apps.payments.providers import sandbox_active as payments_sandbox_active
from apps.reports import analytics as report_analytics
from apps.reports import services as report_services
from apps.rewards import services as rewards
from apps.support import services as support
from apps.rewards.models import DemoReward, RewardPolicy
from apps.audit.models import AuditEvent
from apps.core.models import Job
from apps.core.services import can_change_site_configuration
from apps.memberships import services as memberships
from apps.memberships.forms import ComplimentaryConfirmForm, ComplimentaryGrantForm
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


def _earnings_console():
    from apps.earnings import console as module

    return module


def _earnings_funding():
    from apps.earnings import funding as module

    return module


def _earnings_services():
    from apps.earnings import services as module

    return module


def _withdrawals_real():
    from apps.withdrawals import real as module

    return module


def console_links(user):
    """Navigation entries; each target enforces its own permission too."""
    can = {
        "members": is_super_admin(user) or user.has_perm("accounts.view_user"),
        "plans": is_super_admin(user),
        "videos": videos.can_use_video_console(user),
        "categories": videos.can_manage_categories(user),
        "settings": can_change_site_configuration(user),
        "audit": can_view_audit(user),
        "advertisers": advertising.can_review_advertisers(user) or advertising.can_manage_campaigns(user),
        "campaigns": advertising.can_review_campaigns(user) or advertising.can_manage_campaigns(user),
        # UI-DASH-03: create and manage campaigns on behalf of advertisers (separate from reviewing).
        "campaign_manage": advertising.can_manage_campaigns(user),
        "viewing": viewing.can_review_viewing(user),
        "rewards": rewards.can_use_rewards_console(user),
        # FIN-01: real TZS rewards, campaign funding and earning limits.
        "video_rewards": _earnings_console().can_open_video_rewards(user),
        "campaign_funding": _earnings_funding().can_view_funding(user),
        "earning_rules": _earnings_services().can_view(user) or _earnings_services().can_manage_rules(user),
        # FIN-02: real withdrawals, payout processing, settings and identity checks.
        "withdrawal_requests": _withdrawals_real().can_view(user),
        "withdrawal_settings": _withdrawals_real().can_manage_settings(user),
        "withdrawal_identities": _withdrawals_real().can_verify_identity(user),
        "payments": payments.can_view_payments(user) and payments_sandbox_active(),
        # PAY-01: real membership payments (Airtel Lipa), verified by staff.
        "membership_payments": manual_payments.manual_payments_enabled() and manual_payments.can_view(user),
        # ADMIN-01: the methods page opens for Super Administrators whether or not manual payments are
        # switched on (it says when they are off), so its link follows the page's own check. The
        # verification queue above refuses access while switched off, so its link stays hidden then.
        "payment_instructions": manual_payments.can_manage_instructions(user),
        "wallets": wallet_services.can_view_wallets(user),
        "withdrawals": withdrawals.can_view(user),
        "support": support.can_view_queue(user),
        "reports": bool(report_services.visible_reports(user)),
        "analytics": report_analytics.can_view(user),
    }
    return can


@console
class OverviewView(View):
    """Executive overview: only sections the staff member may see (read-only)."""

    def get(self, request):
        can, sections = admin_summary.overview(request.user)
        return render(request, "administration/overview.html", {
            "nav": "overview", "sections": sections, "can": can, "provider": settings.VIDEO_PROVIDER,
            "attention": admin_summary.attention_items(sections)})


@console
class HealthView(View):
    def get(self, request):
        checks = admin_summary.health(request.user)
        return render(request, "administration/health.html", {
            "nav": "health", "checks": checks, "can": console_links(request.user),
            "client_ip": admin_summary.client_ip_diagnostics(request) if is_super_admin(request.user) else None,
            "breadcrumbs": [("Overview", "administration:overview")]})


@console
class MemberDirectoryView(View):
    def get(self, request):
        can = console_links(request.user)
        if not can["members"]:
            raise PermissionDenied
        query = request.GET.get("q", "").strip()[:254]
        page = Paginator(admin_summary.member_directory(query), 25).get_page(request.GET.get("page"))
        params = request.GET.copy()
        params.pop("page", None)
        return render(request, "administration/members/list.html", {
            "page": page, "query": query, "page_query": params.urlencode(), "can": can, "nav": "members",
            "breadcrumbs": [("Overview", "administration:overview")]})


@console
class MemberDetailView(View):
    def get(self, request, pk):
        can = console_links(request.user)
        if not can["members"]:
            raise PermissionDenied
        User = get_user_model()
        member = get_object_or_404(User, pk=pk, role=User.Role.MEMBER)
        from apps.core.dashboard import member_activity
        from apps.memberships.access import check_access
        from apps.wallets.models import Wallet

        from . import memberships as membership_console

        return render(request, "administration/members/detail.html", {
            "member": member, "membership": check_access(member).subscription,
            "subscriptions": member.subscriptions.order_by("-created_at")[:5],
            # PAY-01B: manual (Airtel Lipa) payment history, for staff allowed to see payments.
            "manual_claims": (ManualMembershipPayment.objects.filter(member=member).order_by("-created_at")[:10]
                              if can["membership_payments"] else None),
            "wallet": Wallet.objects.filter(member=member).first() if can["wallets"] else None,
            "activity": member_activity(member, limit=15),
            "can_change": request.user.has_perm("accounts.change_user"), "can": can, "nav": "members",
            "can_grant": memberships.can_grant_complimentary(request.user),
            # PAY-LIVE-01D: membership details, history, audit trail and permitted actions.
            **membership_console.member_detail_context(request.user, member, can),
            "breadcrumbs": [("Overview", "administration:overview"), ("Members", "administration:members")]})


@console
class ComplimentaryGrantView(View):
    """Time-limited complimentary demo membership (owner decision D1): Super
    Administrators only, review then confirm. memberships.grant_complimentary
    re-checks everything; this view adds no rule of its own."""

    template_name = "administration/members/complimentary.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not memberships.can_grant_complimentary(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def _member(self, pk):
        User = get_user_model()
        return get_object_or_404(User, pk=pk, role=User.Role.MEMBER)

    def _render(self, request, member, form, *, review=None, status=200):
        from apps.memberships.access import current_subscription
        from apps.memberships.models import Subscription

        from apps.memberships.admin_forms import adapt_grant_form

        eligible, reason = memberships.member_eligibility(member)
        mode = memberships.complimentary_grant_mode()  # PAY-LIVE-01E: demo or production wording.
        return render(request, self.template_name, {
            "grant_mode": mode, "max_days": memberships.PRODUCTION_GRANT_MAX_DAYS,
            "member": member, "form": adapt_grant_form(form, mode), "review": review, "eligible": eligible,
            "eligibility_reason": reason,
            "current": current_subscription(member),
            "pending": Subscription.objects.filter(member=member, status=Subscription.Status.PENDING).first(),
            "time_zone": timezone.get_current_timezone_name(), "can": console_links(request.user),
            "nav": "members",
            "breadcrumbs": [("Overview", "administration:overview"), ("Members", "administration:members")],
        }, status=status)

    def get(self, request, pk):
        member = self._member(pk)
        return self._render(request, member, ComplimentaryGrantForm(initial={"operation": uuid.uuid4().hex}))

    def post(self, request, pk):
        member = self._member(pk)
        confirming = request.POST.get("step") == "confirm"
        form = (ComplimentaryConfirmForm if confirming else ComplimentaryGrantForm)(request.POST)
        if not form.is_valid():
            if confirming and set(form.errors) == {"confirm"}:
                return self._render(request, member, form, review=form.cleaned_data, status=400)
            return self._render(request, member, form, status=400)
        if not confirming:  # Review only: nothing is changed until the confirmed second POST.
            return self._render(request, member, form, review=form.cleaned_data)
        data = form.cleaned_data
        try:
            subscription = memberships.grant_complimentary(
                request.user, member, data["plan"], expires_at=data["expires_at"], reason=data["reason"],
                operation_id=data["operation"])
        except memberships.NotAuthorized:
            raise PermissionDenied from None
        except (ValidationError, memberships.MembershipError) as error:
            _form_errors(form, error)
            return self._render(request, member, form, status=400)
        messages.success(request, _("Complimentary %(plan)s membership granted until %(end)s. No payment was "
                                    "recorded.") % {"plan": subscription.plan_name,
                                                    "end": timezone.localtime(subscription.expires_at)
                                                    .strftime("%Y-%m-%d %H:%M %Z")})
        return redirect("administration:member", pk=member.pk)


@console
class SecurityView(View):
    def get(self, request):
        can = console_links(request.user)
        if not can["audit"]:
            raise PermissionDenied
        return render(request, "administration/security.html", {
            "summary": admin_summary.security_summary(), "can": can, "nav": "security",
            "breadcrumbs": [("Overview", "administration:overview")]})


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
        if not (advertising.can_review_advertisers(request.user) or advertising.can_manage_campaigns(request.user)):
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
            "report": advertising.organization_report(organization, for_advertiser=False),  # Staff: exact counts.
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
            elif action == "invite":  # UI-DASH-03: invite the client's people (staff managing campaigns).
                advertising.invite_member(request.user, organization, request.POST.get("email", ""),
                                          request.POST.get("role", "OWNER"))
                messages.success(request, _("Invitation created. The person sees it after signing in with that "
                                            "email address."))
            else:
                raise PermissionDenied
        except advertising.NotAuthorized as error:
            if error.code != "self_approval":
                raise PermissionDenied from None
            messages.error(request, _("A different reviewer must verify a client organization you registered."))
        except (ValidationError, advertising.AdvertisingError) as error:
            messages.error(request, _error_text(error))
        return redirect("administration:advertiser_review", pk=pk)


@console
class CampaignQueueView(View):
    def get(self, request):
        if not (advertising.can_review_campaigns(request.user) or advertising.can_manage_campaigns(request.user)):
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
            "page_query": params.urlencode(), "can": console_links(request.user), "nav": "campaigns",
            "tabs": CAMPAIGN_TABS, "can_report": report_services.can_view(request.user, "advertising")})


# UI-DASH-03: one-click lists in the campaign queue (the status filter keeps every other status).
CAMPAIGN_TABS = [(Campaign.Status.SUBMITTED, _("Awaiting review")), (Campaign.Status.ACTIVE, _("Active")),
                 (Campaign.Status.SCHEDULED, _("Scheduled")), (Campaign.Status.PAUSED, _("Paused")),
                 (Campaign.Status.DRAFT, _("Drafts")), (Campaign.Status.COMPLETED, _("Completed")), ("all", _("All"))]
STAFF_CAMPAIGN_ACTIONS = {"approve", "reject", "pause", "resume", "cancel", "archive"}
# Staff acting for the organization (advertising.manage_campaigns); the services re-check.
STAFF_EDITOR_ACTIONS = {"submit": advertising.submit_campaign, "withdraw": advertising.withdraw_campaign,
                        "reopen": advertising.reopen_campaign}


@console
class CampaignReviewView(View):
    def get(self, request, pk):
        can_review = advertising.can_review_campaigns(request.user)
        can_manage = advertising.can_manage_campaigns(request.user)
        if not (can_review or can_manage):
            raise PermissionDenied
        campaign = get_object_or_404(Campaign.objects.select_related("organization", "video", "video__category",
                                                                     "created_by"), pk=pk)
        preview_reason, preview = advertising.preview_playback(request.user, campaign)
        eligibility = advertising.campaign_eligibility(campaign)
        return render(request, "administration/campaigns/detail.html", {
            "campaign": campaign, "plans": campaign.target_plans.order_by("name"),
            "eligibility": eligibility, "eligibility_message": advertising.eligibility_message(eligibility.reason),
            "video_authorized": advertising.video_authorized(campaign.organization, campaign.video),
            "video_available": advertising.video_available(campaign.video),
            "preview": preview, "preview_reason": preview_reason,
            "performance": advertising.campaign_report(campaign, for_advertiser=False),
            "can_review": can_review, "can_manage": can_manage,
            "created_by_staff": bool(campaign.created_by and campaign.created_by.is_staff),
            # Four eyes: the staff member who created a campaign cannot approve it.
            "can_approve": can_review and campaign.created_by_id != request.user.pk,
            "can": console_links(request.user), "nav": "campaigns"})

    def post(self, request, pk):
        campaign = get_object_or_404(Campaign, pk=pk)
        action = request.POST.get("action", "")
        if action in STAFF_EDITOR_ACTIONS:
            try:
                STAFF_EDITOR_ACTIONS[action](request.user, campaign)
                messages.success(request, _("Campaign updated."))
            except advertising.NotAuthorized:
                raise PermissionDenied from None
            except (ValidationError, advertising.AdvertisingError) as error:
                messages.error(request, _error_text(error))
            return redirect("administration:campaign_review", pk=pk)
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
        except advertising.NotAuthorized as error:
            if error.code != "self_approval":
                raise PermissionDenied from None
            messages.error(request, _("A different reviewer must approve a campaign you created."))
        except (ValidationError, advertising.AdvertisingError) as error:
            messages.error(request, _error_text(error))
        return redirect("administration:campaign_review", pk=pk)


@console
class StaffCampaignFormView(View):
    """UI-DASH-03: create (or edit a draft of) a campaign on behalf of an advertiser organization.
    The staff member is the recorded actor; organization ownership is unchanged."""

    template_name = "administration/campaigns/form.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not advertising.can_manage_campaigns(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def campaign(self, pk):
        return get_object_or_404(Campaign.objects.select_related("organization"), pk=pk) if pk else None

    def render_form(self, request, form, campaign):
        return render(request, self.template_name, {
            "form": form, "campaign": campaign, "can": console_links(request.user),
            "nav": "campaign_new" if campaign is None else "campaigns"})

    def get(self, request, pk=None):
        from apps.advertising.forms import StaffCampaignForm

        campaign = self.campaign(pk)
        if campaign is not None and not campaign.is_editable:
            messages.error(request, _("Only draft campaigns can be edited. Reopen it as a draft first."))
            return redirect("administration:campaign_review", pk=pk)
        organization = None
        if campaign is None and request.GET.get("organization"):
            try:
                organization = Organization.objects.filter(pk=uuid.UUID(request.GET["organization"])).first()
            except ValueError:
                organization = None
        form = (StaffCampaignForm(organization=organization) if campaign is None else
                StaffCampaignForm(instance=campaign, organization=campaign.organization,
                                  initial={"target_plans": campaign.target_plans.all()}))
        return self.render_form(request, form, campaign)

    def post(self, request, pk=None):
        from django.db import transaction

        from apps.advertising.forms import StaffCampaignForm

        campaign = self.campaign(pk)
        form = StaffCampaignForm(request.POST, instance=campaign,
                                 organization=campaign.organization if campaign else None)
        if form.is_valid():
            organization = campaign.organization if campaign else form.cleaned_data["organization"]
            values, plans = form.values()
            try:
                with transaction.atomic():
                    if not advertising.video_authorized(organization, values["video"]):
                        advertising.grant_video(request.user, organization, values["video"])
                    if campaign is None:
                        campaign = advertising.create_campaign(request.user, organization, target_plans=plans,
                                                               **values)
                        messages.success(request, _("Campaign saved as a draft for %(name)s.")
                                         % {"name": organization.display_name})
                    else:
                        try:
                            expected = int(request.POST.get("revision", ""))
                        except ValueError:
                            expected = -1
                        advertising.update_campaign(request.user, campaign, values, expected_revision=expected,
                                                    target_plans=plans)
                        messages.success(request, _("Campaign saved."))
                return redirect("administration:campaign_review", pk=campaign.pk)
            except advertising.NotAuthorized:
                form.add_error("video", _("You are not allowed to authorize this video for the advertiser. "
                                          "Ask a colleague who manages advertiser videos."))
            except (ValidationError, advertising.AdvertisingError) as error:
                _form_errors(form, error)
        return self.render_form(request, form, campaign)


@console
class ClientOrganizationCreateView(View):
    """UI-DASH-03: register a client organization that has no ClipCash account yet."""

    template_name = "administration/advertisers/form.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not advertising.can_manage_campaigns(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def render_form(self, request, form):
        return render(request, self.template_name, {"form": form, "can": console_links(request.user),
                                                    "nav": "advertisers"})

    def get(self, request):
        from apps.advertising.forms import OrganizationForm

        return self.render_form(request, OrganizationForm())

    def post(self, request):
        from apps.advertising.forms import OrganizationForm

        form = OrganizationForm(request.POST)
        if form.is_valid():
            try:
                organization = advertising.create_client_organization(request.user, **form.values())
                messages.success(request, _("Client organization registered. Another reviewer verifies it."))
                return redirect("administration:advertiser_review", pk=organization.pk)
            except (ValidationError, advertising.AdvertisingError) as error:
                _form_errors(form, error)
        return self.render_form(request, form)


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
            qs = qs.filter(identifier_q(filters["member"], "member__"))
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
                                           notes=request.POST.get("notes", ""),
                                           kind=request.POST.get("kind") or RewardPolicy.Kind.CREDITS)
            messages.success(request, _("Policy draft saved. Another administrator must approve it."))
            return redirect("administration:reward_policy", pk=policy.pk)
        except rewards.NotAuthorized:
            raise PermissionDenied from None
        except (ValidationError, rewards.RewardsError) as error:
            messages.error(request, _error_text(error))
        return redirect("administration:reward_policies")


@console
class CampaignCreditsView(View):
    """CR-01: a sponsored campaign's credit rules: eligible plans and total credit budget (set
    here, audited), credits per verified view (versioned policy, four-eyes approval)."""

    template_name = "administration/rewards/campaign_credits.html"

    def _context(self, request, campaign):
        from apps.memberships.models import MembershipPlan
        from apps.rewards.terms import effective_terms

        terms = effective_terms(campaign)
        policy, credits = rewards.current_offer(campaign)
        stored = getattr(campaign, "credit_terms", None) if terms.configured else None
        same_video = []
        for other in (Campaign.objects.filter(video=campaign.video).exclude(pk=campaign.pk)
                      .select_related("organization").order_by("name")[:20]):
            same_video.append({"campaign": other, "credits": rewards.current_offer(other)[1]})
        return {
            "campaign": campaign, "terms": terms, "terms_version": stored.version if stored else 0,
            "policy": policy, "credits": credits, "budget": rewards.budget_summary(campaign),
            "plans": MembershipPlan.objects.filter(status=MembershipPlan.Status.ACTIVE).order_by("display_order",
                                                                                                "name"),
            "eligible_names": sorted(MembershipPlan.objects.filter(pk__in=terms.plan_ids).values_list("name",
                                                                                                    flat=True)),
            "requested_plans": campaign.target_plans.order_by("name"),
            "drafts": RewardPolicy.objects.filter(campaign=campaign, status=RewardPolicy.Status.DRAFT),
            "policies": RewardPolicy.objects.filter(campaign=campaign).order_by("-version")[:10],
            "same_video": same_video,
            "history": AuditEvent.objects.filter(action="rewards.credit_terms.updated",
                                                 target_id=str(campaign.pk)).order_by("-occurred_at")[:20],
            "can_manage": rewards.can_manage_policies(request.user),
            "can": console_links(request.user), "nav": "rewards"}

    def _campaign(self, pk):
        return get_object_or_404(Campaign.objects.select_related("organization", "video"), pk=pk)

    def get(self, request, pk):
        if not rewards.can_use_rewards_console(request.user):
            raise PermissionDenied
        return render(request, self.template_name, self._context(request, self._campaign(pk)))

    def post(self, request, pk):
        campaign = self._campaign(pk)
        try:
            rewards.update_credit_terms(
                request.user, campaign, credit_budget=request.POST.get("credit_budget", "").strip(),
                plan_ids=[value for value in request.POST.getlist("plans") if value],
                expected_version=request.POST.get("expected_version", "0"))
            messages.success(request, _("Credit terms saved. Past awards are not changed."))
        except rewards.NotAuthorized:
            raise PermissionDenied from None
        except (ValidationError, rewards.RewardsError) as error:
            messages.error(request, _error_text(error))
        return redirect("administration:campaign_credits", pk=pk)


def _whole_number(request, name):
    raw = request.POST.get(name, "").strip()
    if not raw.isdigit():
        raise ValidationError({name: _("Enter a whole number of credits.")})
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
            qs = qs.filter(identifier_q(filters["member"], "member__"))
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


# --- PAY-01B: membership plans (prices and durations) -------------------------------------------


def _require_plan_admin(user):
    if not is_super_admin(user):
        raise PermissionDenied


def _plan_queryset():
    from apps.memberships.models import MembershipPlan

    now = timezone.now()
    return MembershipPlan.objects.annotate(
        current_members=Count("subscriptions", filter=Q(subscriptions__status=Subscription.Status.ACTIVE,
                                                        subscriptions__starts_at__lte=now,
                                                        subscriptions__expires_at__gt=now), distinct=True),
        pending_choices=Count("subscriptions", filter=Q(subscriptions__status=Subscription.Status.PENDING),
                              distinct=True),
        open_payments=Count("subscriptions__manual_payments",
                            filter=Q(subscriptions__manual_payments__status="PENDING_VERIFICATION"), distinct=True),
    )


PLAN_ERRORS = {"stale_edit": _("Another administrator changed this plan's price or duration while you were "
                               "editing. Your changes were not saved. The current terms are shown above the "
                               "form; check them and save again.")}


@console
class PlanListView(View):
    def get(self, request):
        _require_plan_admin(request.user)
        return render(request, "administration/plans/list.html", {
            "plans": _plan_queryset().order_by("display_order", "price_amount", "name"),
            "max_price": settings.MEMBERSHIPS_MAX_PLAN_PRICE,
            "can": console_links(request.user), "nav": "plans"})


@console
class PlanCreateView(View):
    template_name = "administration/plans/form.html"

    def _render(self, request, form, status=200):
        return render(request, self.template_name, {"form": form, "plan": None, "can": console_links(request.user),
                                                    "nav": "plans"}, status=status)

    def get(self, request):
        from apps.memberships.forms import ConsolePlanForm

        _require_plan_admin(request.user)
        return self._render(request, ConsolePlanForm(initial={"display_order": 100, "is_public": True}))

    def post(self, request):
        from apps.memberships.forms import ConsolePlanForm

        _require_plan_admin(request.user)
        form = ConsolePlanForm(request.POST)
        if not form.is_valid():
            return self._render(request, form, status=400)
        try:
            plan = memberships.create_plan(request.user, code=form.cleaned_data["code"], **form.plan_values())
        except memberships.NotAuthorized:
            raise PermissionDenied from None
        except ValidationError as error:
            _form_errors(form, error)
            return self._render(request, form, status=400)
        messages.success(request, _("%(plan)s plan created") % {"plan": plan.name}, extra_tags="feedback-auto")
        return redirect("administration:plan", pk=plan.pk)


@console
class PlanEditView(View):
    template_name = "administration/plans/form.html"

    def _render(self, request, plan, form, status=200):
        return render(request, self.template_name, {
            "form": form, "plan": plan, "versions": plan.versions.order_by("-version")[:20],
            "counts": _plan_queryset().get(pk=plan.pk), "statuses": plan.Status,
            "history": AuditEvent.objects.filter(target_model="membershipplan", target_id=str(plan.pk))
                                         .order_by("-occurred_at")[:30],
            "can": console_links(request.user), "nav": "plans",
            # PAY-LIVE-01C: the members' purchase state (manual payments), not the gateway mode.
            "purchase_state": manual_payments.purchase_state()}, status=status)

    def _form(self, plan, data=None):
        from apps.memberships.forms import ConsolePlanEditForm

        return ConsolePlanEditForm(data, instance=plan, initial={"expected_version": plan.version})

    def get(self, request, pk):
        from apps.memberships.models import MembershipPlan

        _require_plan_admin(request.user)
        plan = get_object_or_404(MembershipPlan, pk=pk)
        return self._render(request, plan, self._form(plan))

    def post(self, request, pk):
        from apps.memberships.models import MembershipPlan

        _require_plan_admin(request.user)
        plan = get_object_or_404(MembershipPlan, pk=pk)
        action = request.POST.get("action", "save")
        try:
            if action == "status":
                status = request.POST.get("status", "")
                memberships.set_plan_status(request.user, plan, status)
                messages.success(request, _("Plan status changed."))
                return redirect("administration:plan", pk=pk)
            if action != "save":
                raise PermissionDenied
            original = MembershipPlan.objects.get(pk=pk)
            form = self._form(MembershipPlan.objects.get(pk=pk), request.POST)
            if not form.is_valid():
                return self._render(request, original, form, status=400)
            values = form.plan_values()
            changes = {name: value for name, value in values.items() if getattr(original, name) != value}
            saved_plan = memberships.update_plan(request.user, original, changes,
                                                 expected_version=form.cleaned_data["expected_version"])
        except memberships.NotAuthorized:
            raise PermissionDenied from None
        except ValidationError as error:
            if action == "status":
                messages.error(request, _error_text(error))
                return redirect("administration:plan", pk=pk)
            _form_errors(form, error)
            return self._render(request, MembershipPlan.objects.get(pk=pk), form, status=400)
        except memberships.MembershipError as error:
            messages.error(request, PLAN_ERRORS.get(error.code, str(error)))
            if action == "status":
                return redirect("administration:plan", pk=pk)
            current = MembershipPlan.objects.get(pk=pk)
            if error.code == "stale_edit":
                # Keep the administrator's input, but acknowledge the current version so a deliberate
                # second save (after checking the current terms shown above the form) can succeed.
                data = request.POST.copy()
                data["expected_version"] = current.version
                form = self._form(current, data)
            return self._render(request, current, form, status=400)
        messages.success(request, _("%(plan)s plan saved.") % {"plan": saved_plan.name})
        return redirect("administration:plan", pk=pk)


# --- PAY-01: manual membership payments (Airtel Lipa), REAL money ------------------------------


def _require_manual_payments(user):
    if not manual_payments.manual_payments_enabled() or not manual_payments.can_view(user):
        raise PermissionDenied


@console
class MembershipPaymentQueueView(View):
    """Payments waiting for verification (oldest first), and decided ones."""

    def get(self, request):
        _require_manual_payments(request.user)
        status = request.GET.get("status", ManualMembershipPayment.Status.PENDING_VERIFICATION)
        if status not in ManualMembershipPayment.Status.values and status != "all":
            status = ManualMembershipPayment.Status.PENDING_VERIFICATION
        qs = manual_payments.queue(None if status == "all" else status)
        query = request.GET.get("q", "").strip()[:60]
        if query:
            key = "".join(ch for ch in query.upper() if ch.isalnum())
            match = Q(reference__iexact=query) | identifier_q(query, "member__")
            if key:
                match |= Q(reference_key__contains=key)
            qs = qs.filter(match)
        page = Paginator(qs, 25).get_page(request.GET.get("page"))
        params = request.GET.copy()
        params.pop("page", None)
        # UX-01: the queue's three filters, each with its count (one grouped query).
        S = ManualMembershipPayment.Status
        counts = {row["status"]: row["n"] for row in ManualMembershipPayment.objects
                  .filter(status__in=[S.PENDING_VERIFICATION, S.APPROVED, S.REJECTED])
                  .values("status").annotate(n=Count("pk")).order_by()}
        tabs = [(value, label, counts.get(value, 0)) for value, label in (
            (S.PENDING_VERIFICATION, _("Pending")), (S.APPROVED, _("Approved")), (S.REJECTED, _("Rejected")))]
        return render(request, "administration/membership_payments/list.html", {
            "page": page, "status": status, "q": query, "tabs": tabs,
            "page_query": params.urlencode(), "instruction": manual_payments.active_instruction(),
            "pending_count": counts.get(S.PENDING_VERIFICATION, 0),
            "can": console_links(request.user), "nav": "membership_payments"})


@console
class MembershipPaymentDetailView(View):
    template_name = "administration/membership_payments/detail.html"

    def _render(self, request, claim, approve_form=None, reject_form=None, status=200):
        from apps.payments.forms import ApproveManualPaymentForm, RejectManualPaymentForm
        from apps.wallets.models import JournalEntry

        # PAY-UX-01: claims without a reference share no transaction; only real references are compared.
        same_reference = (ManualMembershipPayment.objects.filter(method=claim.method, reference_key=claim.reference_key)
                          .exclude(pk=claim.pk).select_related("member").order_by("-created_at")[:10]
                          if claim.reference_key else [])
        from apps.memberships.entitlements import label as entitlement_label

        evidence = getattr(claim, "evidence", None)  # PRD-04: optional receipt; never proof.
        entitlements = list(claim.subscription.entitlements or [])
        return render(request, self.template_name, {
            "claim": claim, "subscription": claim.subscription, "member": claim.member,
            "evidence": evidence,
            "evidence_duplicates": list(manual_payments.evidence_duplicates(evidence)[:10]) if evidence else [],
            # UX-01: what activation unlocks, and a warning when the plan unlocks no videos at all.
            "entitlement_labels": [entitlement_label(code) for code in entitlements],
            "unlocks_videos": any(code.startswith("videos.") for code in entitlements),
            "approve_form": approve_form or ApproveManualPaymentForm(instruction=claim.instruction,
                                                                     reference_given=bool(claim.reference_key),
                                                                     amount=claim.amount),
            "reject_form": reject_form or RejectManualPaymentForm(),
            "can_verify": manual_payments.can_verify(request.user) and claim.member_id != request.user.pk,
            "same_reference": same_reference,
            "member_claims": (ManualMembershipPayment.objects.filter(member=claim.member).exclude(pk=claim.pk)
                              .order_by("-created_at")[:10]),
            "ledger_entry": (JournalEntry.objects.filter(pk=claim.ledger_entry_id).first()
                             if claim.ledger_entry_id else None),
            "history": AuditEvent.objects.filter(target_model="manualmembershippayment", target_id=str(claim.pk))
                                         .order_by("occurred_at")[:100],
            "can": console_links(request.user), "nav": "membership_payments",
        }, status=status)

    def _claim(self, pk):
        return get_object_or_404(ManualMembershipPayment.objects.select_related(
            "member", "subscription", "subscription__plan", "instruction", "reviewed_by"), pk=pk)

    def get(self, request, pk):
        _require_manual_payments(request.user)
        return self._render(request, self._claim(pk))

    def post(self, request, pk):
        from apps.payments.forms import ApproveManualPaymentForm, RejectManualPaymentForm

        _require_manual_payments(request.user)
        claim = self._claim(pk)
        action = request.POST.get("action", "")
        if action not in ("approve", "reject"):
            raise PermissionDenied
        # UX-01: authorization comes before form validation, so a viewer is always refused (403),
        # whatever they submitted. The service checks it again inside its transaction.
        if not manual_payments.can_verify(request.user):
            manual_payments.refuse_unauthorized(action, request.user, claim)
            raise PermissionDenied
        if action == "approve":
            form = ApproveManualPaymentForm(request.POST, instruction=claim.instruction,
                                            reference_given=bool(claim.reference_key), amount=claim.amount)
            if not form.is_valid():
                return self._render(request, claim, approve_form=form, status=400)
            try:
                approved, changed = manual_payments.approve(request.user, claim, **form.cleaned_data)
            except manual_payments.NotAuthorized:
                raise PermissionDenied from None
            except ValidationError as error:
                _form_errors(form, error)
                return self._render(request, claim, approve_form=form, status=400)
            except manual_payments.ManualPaymentError as error:
                messages.error(request, str(error))
                return redirect("administration:membership_payment", pk=pk)
            if changed:
                subscription = approved.subscription
                subscription.refresh_from_db()
                dates = {"plan": approved.plan_name,
                         "start": date_format(timezone.localtime(subscription.starts_at), "DATE_FORMAT"),
                         "end": date_format(timezone.localtime(subscription.expires_at), "DATE_FORMAT")}
                if subscription.starts_at > timezone.now():  # Queued after the member's current membership.
                    text = _("Payment confirmed. %(plan)s starts on %(start)s, when the current membership ends, "
                             "and runs until %(end)s. The member has been notified.") % dates
                else:
                    text = _("Payment confirmed. %(plan)s membership is active now, until %(end)s. The member has "
                             "been notified.") % dates
                messages.success(request, text)
            else:
                messages.success(request, _("This payment was already confirmed. Nothing changed."))
        else:
            form = RejectManualPaymentForm(request.POST)
            if not form.is_valid():
                return self._render(request, claim, reject_form=form, status=400)
            try:
                _claim, changed = manual_payments.reject(request.user, claim, **form.cleaned_data)
            except manual_payments.NotAuthorized:
                raise PermissionDenied from None
            except ValidationError as error:
                _form_errors(form, error)
                return self._render(request, claim, reject_form=form, status=400)
            except manual_payments.ManualPaymentError as error:
                messages.error(request, str(error))
                return redirect("administration:membership_payment", pk=pk)
            messages.success(request, _("Payment rejected. The member was notified and can send corrected details.") if changed
                             else _("This payment was already rejected. Nothing changed."))
        return redirect("administration:membership_payment", pk=pk)


def _require_instruction_admin(user):
    """Receiving numbers decide where members' money goes: Super Administrators only."""
    if not manual_payments.can_manage_instructions(user):
        raise PermissionDenied


def _instruction_or_404(pk):
    from apps.payments.models import ManualPaymentInstruction

    return get_object_or_404(ManualPaymentInstruction.objects.select_related("created_by", "retired_by"), pk=pk)


@console
class PaymentInstructionsView(View):
    """UX-02: the payment methods workspace. Read-only: adding, editing, retiring and
    reactivating each have their own page with an explicit confirmation."""

    http_method_names = ["get", "head", "options"]

    def dispatch(self, request, *args, **kwargs):
        _require_instruction_admin(request.user)  # Before the method check: others always get 403.
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        versions = manual_payments.instruction_versions()
        return render(request, "administration/membership_payments/instructions.html", {
            "methods": [row for row in versions if row.state == "active"],
            "retired": [row for row in versions if row.state == "retired"],
            "versions": versions,
            "enabled": manual_payments.manual_payments_enabled(),
            "can": console_links(request.user), "nav": "payment_instructions"})


@console
class PaymentMethodFormView(View):
    """UX-02: add a provider, or publish new details for an active one. Two steps: the form is
    reviewed first, then published only after the administrator confirms the details. Publishing
    always creates a new version; issued instructions and claims keep the version they used."""

    template_name = "administration/membership_payments/method_form.html"

    def _current(self, pk):
        if pk is None:
            return None
        row = _instruction_or_404(pk)
        if not row.is_active:  # Only the live version is edited; retired ones are reactivated.
            raise Http404
        return row

    def _form(self, current, data=None):
        from apps.payments.forms import PaymentInstructionForm

        if current is None:
            return PaymentInstructionForm(data)
        initial = {name: getattr(current, name) for name in
                   ("provider_label", "business_number", "recipient_name", "instructions")}
        return PaymentInstructionForm(data, initial=initial, fixed_method=current.method)

    def _render(self, request, current, form, *, confirm_form=None, review=False, status=200):
        changes = []
        if review and current is not None:
            for name, label in (("provider_label", _("Name shown to members")),
                                ("business_number", _("Receiving number")),
                                ("recipient_name", _("Registered recipient name")),
                                ("instructions", _("Payment steps for members"))):
                before, after = getattr(current, name), form.cleaned_data[name]
                if before != after:
                    changes.append((label, before, after))
        return render(request, self.template_name, {
            "form": form, "current": current, "review": review, "changes": changes,
            "values": form.cleaned_data if review else None,
            "confirm_form": confirm_form, "can": console_links(request.user), "nav": "payment_instructions",
        }, status=status)

    def get(self, request, pk=None):
        _require_instruction_admin(request.user)
        current = self._current(pk)
        return self._render(request, current, self._form(current))

    def post(self, request, pk=None):
        from apps.payments.forms import PublishConfirmForm
        from apps.payments.models import ManualPaymentInstruction

        _require_instruction_admin(request.user)
        current = self._current(pk)
        form = self._form(current, request.POST)
        step = request.POST.get("step", "review")
        if not form.is_valid() or step == "edit":  # "Change details" returns to the form.
            return self._render(request, current, form, status=200 if step == "edit" else 400)
        values = form.publish_values()
        if current is None and ManualPaymentInstruction.objects.filter(method=values["method"], is_active=True).exists():
            # Adding never silently replaces a live provider's receiving details.
            form.add_error("preset" if "preset" in form.fields and not request.POST.get("method") else "method", _(
                "This provider already has an active payment method. Edit it from the payment methods page "
                "instead."))
            return self._render(request, current, form, status=400)
        if current is not None and all(getattr(current, key) == value for key, value in values.items()):
            form.add_error(None, _("Nothing changed. Edit a detail, or cancel."))
            return self._render(request, current, form, status=400)
        expected = current.pk if current else None
        if step != "confirm":
            confirm_form = PublishConfirmForm(initial={"expected_active": expected})
            return self._render(request, current, form, confirm_form=confirm_form, review=True)
        confirm_form = PublishConfirmForm(request.POST)
        if not confirm_form.is_valid():
            return self._render(request, current, form, confirm_form=confirm_form, review=True, status=400)
        reviewed = confirm_form.cleaned_data["expected_active"]
        if (str(reviewed) if reviewed else None) != (str(expected) if expected else None):
            raise PermissionDenied  # The review step was not for this method.
        try:
            manual_payments.publish_instruction(request.user, expected_active=reviewed, **values)
        except manual_payments.NotAuthorized:
            raise PermissionDenied from None
        except ValidationError as error:
            _form_errors(form, error)
            return self._render(request, current, form, status=400)
        except manual_payments.InvalidState:
            messages.error(request, _("This payment method was changed by someone else while you were reviewing it. "
                                      "Nothing was published. Check the current details and try again."))
            return redirect("administration:payment_instructions")
        messages.success(request, _("%(provider)s payment details published. Members see them from now on; "
                                    "earlier instructions and pending payments keep their original details.")
                         % {"provider": values["provider_label"]})
        return redirect("administration:payment_instructions")


@console
class PaymentMethodStatusView(View):
    """UX-02: retire an active method, or reactivate a retired one, after a confirmation page."""

    template_name = "administration/membership_payments/method_status.html"
    action = None  # "retire" or "reactivate"

    def _row(self, pk):
        row = _instruction_or_404(pk)
        if self.action == "retire" and not row.is_active:
            raise Http404
        if self.action == "reactivate":
            state = next((v.state for v in manual_payments.instruction_versions() if v.pk == row.pk), None)
            if state != "retired":  # Active, or replaced by a newer version: nothing to reactivate.
                raise Http404
        return row

    def get(self, request, pk):
        _require_instruction_admin(request.user)
        row = self._row(pk)
        return render(request, self.template_name, {
            "row": row, "action": self.action, "can": console_links(request.user), "nav": "payment_instructions",
            "pending_claims": ManualMembershipPayment.objects.filter(
                instruction__method=row.method, status=ManualMembershipPayment.Status.PENDING_VERIFICATION).count()})

    def post(self, request, pk):
        _require_instruction_admin(request.user)
        row = self._row(pk)
        try:
            if self.action == "retire":
                manual_payments.retire_instruction(request.user, row.method)
                messages.success(request, _("%(provider)s retired. Members can no longer choose it; payments already "
                                            "submitted can still be reviewed.") % {"provider": row.provider_label})
            else:
                manual_payments.enable_instruction(request.user, row)
                messages.success(request, _("%(provider)s reactivated with the same details. Members can choose it "
                                            "again.") % {"provider": row.provider_label})
        except manual_payments.NotAuthorized:
            raise PermissionDenied from None
        except manual_payments.InvalidState as error:
            messages.error(request, str(error))
        return redirect("administration:payment_instructions")


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


# --- Withdrawals, DEMO (Phase 10) --------------------------------------------------------------

WITHDRAWAL_SIMULATOR = [("succeed", _("Provider: success")), ("fail", _("Provider: failure")),
                        ("timeout", _("Provider: timeout")), ("restore", _("Provider: reachable again")),
                        ("return", _("Provider: payout returned")), ("replay", _("Replay last notice")),
                        ("forged", _("Forged notice"))]


@console
class WithdrawalListView(View):
    def get(self, request):
        if not withdrawals.can_view(request.user):
            raise PermissionDenied
        query = request.GET.get("q", "").strip()[:100]
        status = request.GET.get("status", "")
        flagged = request.GET.get("flagged") == "1"
        page = Paginator(withdrawals.staff_queue(status=status, query=query, flagged=flagged), 25).get_page(
            request.GET.get("page"))
        params = request.GET.copy()
        params.pop("page", None)
        member_email = request.GET.get("member", "").strip()[:254]
        member_lookup, member_beneficiaries = None, []
        if member_email and withdrawals.can_review(request.user):
            member_lookup = get_user_model().objects.filter(email__iexact=member_email, is_staff=False).first()
            if member_lookup:
                member_beneficiaries = PayoutBeneficiary.objects.filter(member=member_lookup, status="ACTIVE")
        return render(request, "administration/withdrawals/list.html", {
            "page": page, "query": query, "status": status, "flagged": flagged, "page_query": params.urlencode(),
            "statuses": DemoWithdrawal.Status.choices, "member_email": member_email, "member_lookup": member_lookup,
            "member_beneficiaries": member_beneficiaries, "form_token": uuid.uuid4().hex,
            "open_exceptions": WithdrawalException.objects.filter(status="OPEN").count(),
            "can_review": withdrawals.can_review(request.user),
            "can_beneficiaries": withdrawals.can_view_beneficiaries(request.user),
            "can": console_links(request.user), "nav": "withdrawals"})

    def post(self, request):
        if not withdrawals.can_view(request.user):
            raise PermissionDenied
        member = get_object_or_404(get_user_model(), pk=request.POST.get("member"))
        beneficiary = get_object_or_404(PayoutBeneficiary, pk=request.POST.get("beneficiary"), member=member)
        try:
            demo = withdrawals.create_demo_withdrawal(request.user, member, beneficiary,
                                                      amount=_whole_number(request, "amount"),
                                                      idempotency_key=request.POST.get("form_token", ""))
            messages.success(request, _("Demo request created."))
            return redirect("administration:withdrawal", pk=demo.pk)
        except withdrawals.NotAuthorized:
            raise PermissionDenied from None
        except (ValidationError, withdrawals.WithdrawalError, PaymentsDisabled) as error:
            messages.error(request, _error_text(error))
        return redirect("administration:withdrawals")


@console
class WithdrawalDetailView(View):
    def get(self, request, pk):
        if not withdrawals.can_view(request.user):
            raise PermissionDenied
        demo = get_object_or_404(DemoWithdrawal.objects.select_related(
            "member", "created_by", "approved_by", "second_approved_by", "payout_initiated_by"), pk=pk)
        return render(request, "administration/withdrawals/detail.html", {
            "demo": demo, "decisions": demo.decisions.select_related("actor"),
            "events": PayoutEvent.objects.filter(payout_reference=demo.payout_reference).order_by("received_at")
                      if demo.payout_reference else [],
            "history": AuditEvent.objects.filter(target_model="demowithdrawal", target_id=str(demo.pk))
                                         .order_by("occurred_at")[:100],
            "reasons": withdrawals.REJECTION_REASONS, "simulator_actions": WITHDRAWAL_SIMULATOR,
            "can_review": withdrawals.can_review(request.user),
            "can_operate": withdrawals.can_operate_payouts(request.user),
            "can": console_links(request.user), "nav": "withdrawals"})

    def post(self, request, pk):
        if not withdrawals.can_view(request.user):
            raise PermissionDenied
        demo = get_object_or_404(DemoWithdrawal, pk=pk)
        action = request.POST.get("action", "")
        expected = request.POST.get("expected_status", "")
        note = request.POST.get("note", "")[:300]
        try:
            if action == "start_review":
                withdrawals.start_review(request.user, demo, expected_status=expected)
            elif action == "approve":
                withdrawals.approve(request.user, demo, expected_status=expected, note=note)
            elif action == "reject":
                withdrawals.reject(request.user, demo, expected_status=expected,
                                   reason_code=request.POST.get("reason_code", ""), note=note)
            elif action == "cancel":
                withdrawals.cancel(request.user, demo, reason_code=request.POST.get("reason_code", ""), note=note)
            elif action == "initiate":
                withdrawals.initiate_payout(request.user, demo, expected_status=expected)
            elif action == "simulate":
                status, outcome = withdrawals.simulate(request.user, demo, request.POST.get("simulate", ""))
                messages.info(request, _("Simulated notice: HTTP %(s)s, %(o)s.") % {"s": status, "o": outcome})
            else:
                raise PermissionDenied
            if action != "simulate":
                messages.success(request, _("Demo withdrawal updated."))
        except withdrawals.NotAuthorized:
            raise PermissionDenied from None
        except (ValidationError, withdrawals.WithdrawalError, PaymentsDisabled) as error:
            messages.error(request, _error_text(error))
        return redirect("administration:withdrawal", pk=pk)


@console
class WithdrawalExceptionsView(View):
    def get(self, request):
        if not withdrawals.can_view(request.user):
            raise PermissionDenied
        qs = WithdrawalException.objects.select_related("withdrawal").order_by("status", "-created_at")
        return render(request, "administration/withdrawals/exceptions.html", {
            "page": Paginator(qs, 25).get_page(request.GET.get("page")),
            "can_review": withdrawals.can_review(request.user), "can": console_links(request.user),
            "nav": "withdrawals"})

    def post(self, request):
        if not withdrawals.can_view(request.user):
            raise PermissionDenied
        exception = get_object_or_404(WithdrawalException, pk=request.POST.get("exception"))
        try:
            withdrawals.resolve_exception(request.user, exception, note=request.POST.get("note", ""))
            messages.success(request, _("Exception resolved."))
        except withdrawals.NotAuthorized:
            raise PermissionDenied from None
        except (ValidationError, withdrawals.WithdrawalError) as error:
            messages.error(request, _error_text(error))
        return redirect("administration:withdrawal_exceptions")


@console
class BeneficiaryListView(View):
    def get(self, request):
        if not withdrawals.can_view_beneficiaries(request.user):
            raise PermissionDenied
        query = request.GET.get("q", "").strip()[:254]
        qs = PayoutBeneficiary.objects.select_related("member").order_by("-created_at")
        if query:
            qs = qs.filter(identifier_q(query, "member__", exact_email=True))
        return render(request, "administration/withdrawals/beneficiaries.html", {
            "page": Paginator(qs, 25).get_page(request.GET.get("page")), "query": query,
            "can": console_links(request.user), "nav": "withdrawals"})
