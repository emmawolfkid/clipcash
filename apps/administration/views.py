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

from apps.audit.admin import can_view_audit
from apps.core.models import Job
from apps.core.services import can_change_site_configuration
from apps.memberships.models import Subscription
from apps.memberships.services import is_super_admin
from apps.notifications.models import OutboundEmail
from apps.videos import services as videos
from apps.videos.forms import CategoryForm, VideoForm
from apps.videos.models import Video, VideoCategory

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
