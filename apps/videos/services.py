"""
Video services: the only supported way to change categories and videos.

* External HTTP calls (Bunny) never run inside a database transaction.
* Remote video creation is claimed with a conditional UPDATE, so concurrent
  requests cannot create two Bunny videos for one record.
* Publication requires READY processing, an active category (if any) and the
  publish permission; the database also enforces READY for PUBLISHED.
* Playback is authorized server-side through apps.memberships.access; only
  short-lived signed player URLs are ever issued.
"""

import time
from datetime import timedelta

import django.dispatch
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from apps.audit.services import DENIED, record_business_event, record_security_event
from apps.core import features
from apps.core.jobs.errors import PermanentJobError, RetryableJobError
from apps.core.jobs.services import enqueue
from apps.memberships.access import check_access

from .models import Video, VideoCategory
from .providers import (
    ProviderError,
    ProviderNotConfigured,
    ProviderPermanentError,
    ProviderTemporaryError,
    default_provider_code,
    demo_allowed,
    get_provider,
)

P = Video.Processing
PUB = Video.Publication

# Phase 06 hook: sent whenever playback is authorized (never a verified view).
playback_authorized = django.dispatch.Signal()


class VideoError(Exception):
    code = "video_error"

    def __init__(self, message, *, code=None):
        super().__init__(message)
        if code:
            self.code = code


class NotAuthorized(VideoError):
    code = "not_authorized"


class InvalidState(VideoError):
    code = "invalid_state"


class ProviderUnavailable(VideoError):
    code = "provider_unavailable"


class UploadRejected(VideoError):
    code = "upload_rejected"


# --- Permissions -----------------------------------------------------------------


def _staff_with(user, perm):
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.is_active
                and user.is_staff and user.has_perm(perm))


def can_manage_videos(user):
    return _staff_with(user, "videos.manage_videos")


def can_publish_videos(user):
    return _staff_with(user, "videos.publish_videos")


def can_manage_categories(user):
    return _staff_with(user, "videos.manage_categories")


def can_use_video_console(user):
    return can_manage_videos(user) or can_publish_videos(user) or can_manage_categories(user)


def _require(check, actor, action, target=None):
    if not check(actor):
        error = NotAuthorized("You are not allowed to do this.")
        record_security_event("video.action.denied", outcome=DENIED,
                              actor=actor if getattr(actor, "is_authenticated", False) else None,
                              target=target, metadata={"action": action, "reason": error.code})
        raise error


# --- Categories --------------------------------------------------------------------

CATEGORY_FIELDS = ("name", "slug", "description", "display_order")


def create_category(actor, **values):
    _require(can_manage_categories, actor, "category.create")
    values = {k: v for k, v in values.items() if k in CATEGORY_FIELDS}
    values["slug"] = values.get("slug") or slugify(values.get("name", ""))
    with transaction.atomic():
        category = VideoCategory(**values)
        category.full_clean()
        category.save()
        record_business_event("video.category.created", actor=actor, target=category,
                              metadata={"slug": category.slug})
    return category


def update_category(actor, category, changes):
    _require(can_manage_categories, actor, "category.update", category)
    changes = {k: v for k, v in changes.items() if k in CATEGORY_FIELDS}
    with transaction.atomic():
        category = VideoCategory.objects.select_for_update().get(pk=category.pk)
        before = {k: getattr(category, k) for k in changes}
        for key, value in changes.items():
            setattr(category, key, value)
        category.full_clean()
        changed = sorted(k for k in changes if getattr(category, k) != before[k])
        if changed:
            category.save()
            record_business_event("video.category.updated", actor=actor, target=category,
                                  metadata={"slug": category.slug, "fields": changed})
    return category


def set_category_active(actor, category, active):
    _require(can_manage_categories, actor, "category.status", category)
    with transaction.atomic():
        category = VideoCategory.objects.select_for_update().get(pk=category.pk)
        if category.is_active != active:
            category.is_active = active
            category.save(update_fields=["is_active", "updated_at"])
            record_business_event("video.category.status_changed", actor=actor, target=category,
                                  metadata={"slug": category.slug, "active": active})
    return category


# --- Videos: metadata ------------------------------------------------------------------

VIDEO_FIELDS = ("title", "slug", "description", "category", "required_entitlement", "duration_seconds")


def _check_category(category):
    if category is not None and not category.is_active:
        raise ValidationError({"category": "Choose an active category."})


def create_video(actor, **values):
    _require(can_manage_videos, actor, "video.create")
    values = {k: v for k, v in values.items() if k in VIDEO_FIELDS}
    _check_category(values.get("category"))
    provider_code = default_provider_code()
    if provider_code == Video.Provider.DEMO and not demo_allowed():
        raise ProviderUnavailable("Demo videos are disabled here.")
    if provider_code == Video.Provider.BUNNY:
        values.pop("duration_seconds", None)  # Comes from Bunny after processing.
    values["slug"] = values.get("slug") or slugify(values.get("title", ""))[:160]
    with transaction.atomic():
        video = Video(provider=provider_code, created_by=actor, **values)
        if provider_code == Video.Provider.DEMO:
            # Clearly labelled sample record: no upload or processing happens.
            video.provider_video_id = get_provider("DEMO").create_video(video.title)
            video.processing_status = P.READY
        video.full_clean()
        video.save()
        record_business_event("video.created", actor=actor, target=video,
                              metadata={"provider": provider_code, "slug": video.slug})
    return video


def update_video(actor, video, changes):
    _require(can_manage_videos, actor, "video.update", video)
    changes = {k: v for k, v in changes.items() if k in VIDEO_FIELDS}
    with transaction.atomic():
        video = Video.objects.select_for_update().get(pk=video.pk)
        if video.publication_status == PUB.ARCHIVED:
            raise InvalidState("Archived videos cannot be edited.")
        if "slug" in changes and changes["slug"] != video.slug and video.published_at is not None:
            raise ValidationError({"slug": "The address of a published video cannot change."})
        if "duration_seconds" in changes and video.provider != Video.Provider.DEMO:
            changes.pop("duration_seconds")
        if "category" in changes and changes["category"] != video.category:
            _check_category(changes["category"])
        before = {k: getattr(video, k) for k in changes}
        for key, value in changes.items():
            setattr(video, key, value)
        video.full_clean()
        changed = sorted(k for k in changes if getattr(video, k) != before[k])
        if changed:
            video.save()
            record_business_event("video.updated", actor=actor, target=video, metadata={"fields": changed})
    return video


# --- Videos: upload and processing ---------------------------------------------------------


def _ensure_remote_video(video):
    """Create the Bunny video once. Runs with no transaction open around the HTTP call."""
    if video.provider_video_id:
        return video
    now = timezone.now()
    stale = now - timedelta(minutes=5)
    claimed = Video.objects.filter(pk=video.pk, provider_video_id="").filter(
        Q(remote_creation_started_at__isnull=True) | Q(remote_creation_started_at__lt=stale)
    ).update(remote_creation_started_at=now)
    if not claimed:
        video.refresh_from_db()
        if video.provider_video_id:
            return video
        raise InvalidState("Upload preparation is already in progress. Try again in a moment.", code="in_progress")
    try:
        provider = get_provider(video.provider)
        remote_id = provider.create_video(video.title)
    except ProviderError as error:
        Video.objects.filter(pk=video.pk).update(remote_creation_started_at=None)
        raise ProviderUnavailable(str(error)) from None
    with transaction.atomic():
        locked = Video.objects.select_for_update().get(pk=video.pk)
        locked.provider_video_id = remote_id
        locked.provider_library_id = getattr(provider, "library_id", "")
        locked.remote_creation_started_at = None
        locked.save(update_fields=["provider_video_id", "provider_library_id", "remote_creation_started_at",
                                   "updated_at"])
    return locked


def prepare_upload(actor, video, *, filename, size, content_type):
    """Authorize one direct browser-to-Bunny upload. Returns UploadAuthorization."""
    _require(can_manage_videos, actor, "video.upload", video)
    video = Video.objects.get(pk=video.pk)
    if video.provider != Video.Provider.BUNNY:
        raise UploadRejected("Demo videos have no uploads.", code="demo")
    if video.publication_status == PUB.ARCHIVED:
        raise InvalidState("Archived videos cannot receive uploads.")
    if video.processing_status not in (P.PENDING, P.UPLOADING, P.FAILED):
        raise InvalidState("This video already has media.", code="has_media")
    if content_type not in settings.VIDEO_UPLOAD_CONTENT_TYPES:
        raise UploadRejected("This file type is not supported.", code="file_type")
    max_bytes = settings.VIDEO_UPLOAD_MAX_MB * 1024 * 1024
    if type(size) is not int or size <= 0 or size > max_bytes:
        raise UploadRejected(f"Files must be smaller than {settings.VIDEO_UPLOAD_MAX_MB} MB.", code="file_size")

    video = _ensure_remote_video(video)
    try:
        authorization = get_provider(video.provider).upload_authorization(
            video.provider_video_id, expires_in=settings.VIDEO_UPLOAD_AUTH_SECONDS,
            title=video.title, filetype=content_type)
    except ProviderError as error:
        raise ProviderUnavailable(str(error)) from None
    with transaction.atomic():
        locked = Video.objects.select_for_update().get(pk=video.pk)
        locked.processing_status = P.UPLOADING
        locked.processing_error = ""
        locked.save(update_fields=["processing_status", "processing_error", "updated_at"])
        record_business_event("video.upload.initiated", actor=actor, target=locked,
                              metadata={"content_type": content_type, "size_mb": max(1, size // (1024 * 1024))})
    return authorization


def report_upload_finished(actor, video):
    _require(can_manage_videos, actor, "video.upload_finished", video)
    with transaction.atomic():
        video = Video.objects.select_for_update().get(pk=video.pk)
        if video.processing_status != P.UPLOADING:
            return video  # Idempotent: already processing/ready.
        video.processing_status = P.PROCESSING
        video.save(update_fields=["processing_status", "updated_at"])
        record_business_event("video.upload.completed", actor=actor, target=video)
        enqueue_sync(video)
    return video


def _notify_video(video, event, title, message):
    """Tell the staff member who created the video (or, if that account is gone, the
    video managers). Once per video, event and status change."""
    from apps.notifications import inbox

    creator = video.created_by if video.created_by_id and video.created_by.is_active else None
    recipients = [creator] if creator else inbox.staff_with_permission("videos.manage_videos")
    inbox.notify(recipients, f"video.{event}", category=inbox.Category.VIDEO, title=title, message=message,
                 link=inbox.link_to("administration:video_detail", video.pk),
                 dedupe_key=f"video.{event}:{video.pk}:{video.updated_at.isoformat() if video.updated_at else ''}")


def report_upload_failed(actor, video, reason):
    _require(can_manage_videos, actor, "video.upload_failed", video)
    reason = reason if reason in ("network", "aborted", "rejected", "unknown") else "unknown"
    with transaction.atomic():
        video = Video.objects.select_for_update().get(pk=video.pk)
        if video.processing_status != P.UPLOADING:
            return video
        video.processing_status = P.FAILED
        video.processing_error = "The upload did not complete. You can try again."
        video.save(update_fields=["processing_status", "processing_error", "updated_at"])
        record_business_event("video.upload.failed", actor=actor, target=video, metadata={"reason": reason})
        _notify_video(video, "failed", "Video upload did not complete",
                      f"“{video.title}” was not uploaded. You can try again.")
    return video


def enqueue_sync(video, delay_seconds=0):
    """One sync job per video per minute (idempotent)."""
    run_after = timezone.now() + timedelta(seconds=delay_seconds)
    return enqueue("videos.sync_status", {"video_id": str(video.pk)}, run_after=run_after,
                   idempotency_key=f"video-sync:{video.pk}:{int(time.time() // 60)}")


def sync_video(video_id):
    """Pull processing status from the provider. Called by a non-transactional job."""
    video = Video.objects.filter(pk=video_id).first()
    if video is None or video.provider != Video.Provider.BUNNY or not video.provider_video_id:
        return "skipped"
    if video.processing_status == P.READY and video.thumbnail_file and video.duration_seconds:
        return "ready"
    try:
        info = get_provider(video.provider).get_video(video.provider_video_id)  # HTTP, no transaction.
    except ProviderTemporaryError as error:
        raise RetryableJobError(str(error)) from None
    except ProviderPermanentError as error:
        raise PermanentJobError(str(error)) from None

    with transaction.atomic():
        video = Video.objects.select_for_update().get(pk=video_id)
        video.provider_status_code = info.status_code
        video.last_synced_at = timezone.now()
        if info.duration_seconds:
            video.duration_seconds = info.duration_seconds
        if info.thumbnail_file:
            video.thumbnail_file = info.thumbnail_file
        outcome = info.processing
        if video.processing_status == P.READY:
            # Already playable (maybe published): refresh metadata only. A later
            # provider report never unpublishes or fails a ready video.
            video.save()
            return P.READY
        if info.processing == P.READY:
            video.processing_status = P.READY
            video.processing_error = ""
            record_business_event("video.processing.completed", actor=None, target=video,
                                  metadata={"duration_seconds": video.duration_seconds or 0})
            _notify_video(video, "ready", "Video is ready", f"“{video.title}” finished processing and can be "
                          "published.")
        elif info.processing == P.FAILED and video.processing_status != P.FAILED:
            video.processing_status = P.FAILED
            video.processing_error = info.error[:200]
            record_business_event("video.processing.failed", actor=None, target=video,
                                  metadata={"status_code": info.status_code})
            _notify_video(video, "failed", "Video processing failed",
                          f"“{video.title}” could not be processed. Open it to upload again.")
        elif info.processing == P.PROCESSING and video.processing_status in (P.PENDING, P.UPLOADING):
            video.processing_status = P.PROCESSING  # Bunny received the upload.
        video.save()
    if outcome in (P.PROCESSING, P.UPLOADING):
        raise RetryableJobError("Still processing at the provider")
    return outcome


def sweep_unfinished(limit=100):
    """Recover videos stuck in UPLOADING/PROCESSING: re-sync, or fail stale uploads."""
    now = timezone.now()
    stale_before = now - timedelta(hours=settings.VIDEO_UPLOAD_STALE_HOURS)
    queued = 0
    candidates = (Video.objects.filter(provider=Video.Provider.BUNNY, processing_status__in=[P.UPLOADING, P.PROCESSING])
                  .exclude(provider_video_id="").order_by("updated_at")[:limit])
    for video in candidates:
        if video.processing_status == P.UPLOADING and video.updated_at < stale_before:
            with transaction.atomic():
                locked = Video.objects.select_for_update().get(pk=video.pk)
                if locked.processing_status == P.UPLOADING:
                    locked.processing_status = P.FAILED
                    locked.processing_error = "No upload was received. You can try again."
                    locked.save(update_fields=["processing_status", "processing_error", "updated_at"])
                    record_business_event("video.upload.failed", actor=None, target=locked,
                                          metadata={"reason": "stale"})
            continue
        enqueue_sync(video)
        queued += 1
    return queued


# --- Publication ------------------------------------------------------------------------


def _publication_change(actor, video, action, check, new_status):
    _require(can_publish_videos, actor, f"video.{action}", video)
    with transaction.atomic():
        video = Video.objects.select_for_update(of=("self",)).select_related("category").get(pk=video.pk)
        check(video)
        old = video.publication_status
        video.publication_status = new_status
        now = timezone.now()
        if new_status == PUB.PUBLISHED and video.published_at is None:
            video.published_at = now
        if new_status == PUB.ARCHIVED:
            video.archived_at = now
        video.save(update_fields=["publication_status", "published_at", "archived_at", "updated_at"])
        record_business_event(f"video.{action}", actor=actor, target=video, metadata={"from_status": old})
    return video


def publish(actor, video):
    def check(v):
        if v.publication_status == PUB.PUBLISHED:
            raise InvalidState("This video is already published.", code="already_published")
        if v.publication_status != PUB.DRAFT:
            raise InvalidState("Only draft videos can be published.")
        if v.processing_status != P.READY:
            raise InvalidState("The video must finish processing before it can be published.", code="not_ready")
        if v.category is not None and not v.category.is_active:
            raise InvalidState("Choose an active category before publishing.", code="inactive_category")
        if v.is_demo and not demo_allowed():
            raise InvalidState("Demo videos cannot be published here.", code="demo")
    return _publication_change(actor, video, "published", check, PUB.PUBLISHED)


def unpublish(actor, video):
    def check(v):
        if v.publication_status != PUB.PUBLISHED:
            raise InvalidState("Only published videos can be unpublished.")
    return _publication_change(actor, video, "unpublished", check, PUB.DRAFT)


def archive(actor, video):
    def check(v):
        if v.publication_status == PUB.ARCHIVED:
            raise InvalidState("This video is already archived.")
    return _publication_change(actor, video, "archived", check, PUB.ARCHIVED)


def request_media_deletion(actor, video):
    """Super Administrator: delete an ARCHIVED video's media from Bunny (job)."""
    from apps.memberships.services import is_super_admin

    _require(is_super_admin, actor, "video.delete_media", video)
    video = Video.objects.get(pk=video.pk)
    if video.publication_status != PUB.ARCHIVED or video.provider != Video.Provider.BUNNY or not video.provider_video_id:
        raise InvalidState("Only archived Bunny videos can have their media deleted.")
    with transaction.atomic():
        enqueue("videos.delete_media", {"video_id": str(video.pk)}, idempotency_key=f"video-delete:{video.pk}")
        record_business_event("video.media.deletion_requested", actor=actor, target=video)


def delete_media(video_id):
    video = Video.objects.get(pk=video_id)
    if video.publication_status != PUB.ARCHIVED:
        raise PermanentJobError("Only archived videos can have media deleted.")
    try:
        get_provider(video.provider).delete_video(video.provider_video_id)  # HTTP, no transaction.
    except ProviderTemporaryError as error:
        raise RetryableJobError(str(error)) from None
    except ProviderPermanentError as error:
        raise PermanentJobError(str(error)) from None
    with transaction.atomic():
        locked = Video.objects.select_for_update().get(pk=video_id)
        locked.processing_status = P.FAILED
        locked.processing_error = "Media deleted from the provider."
        locked.thumbnail_file = ""
        locked.save(update_fields=["processing_status", "processing_error", "thumbnail_file", "updated_at"])
        record_business_event("video.media.deleted", actor=None, target=locked)


# --- Member access and playback ---------------------------------------------------------------


def library_queryset():
    """Videos members may browse: published, ready, active-or-no category."""
    qs = (Video.objects.select_related("category")
          .filter(publication_status=PUB.PUBLISHED, processing_status=P.READY)
          .filter(Q(category__isnull=True) | Q(category__is_active=True)))
    if not demo_allowed():
        qs = qs.exclude(provider=Video.Provider.DEMO)
    return qs


def authorize_playback(user, video):
    """(reason, Playback | None). ``reason`` is "ok" or why playback is refused."""
    if not features.is_enabled("videos"):
        return "feature_disabled", None
    if not video.is_playable or (video.is_demo and not demo_allowed()):
        return "unavailable", None
    decision = check_access(user, entitlement=video.required_entitlement)
    if not decision:
        return decision.reason, None
    try:
        playback = get_provider(video.provider).playback(video.provider_video_id,
                                                         expires_in=settings.VIDEO_PLAYBACK_TOKEN_SECONDS)
    except (ProviderError, ProviderNotConfigured):
        return "unavailable", None
    playback_authorized.send(sender=Video, user=user, video=video, expires_at=playback.expires_at)
    return "ok", playback


def thumbnail_url(video):
    if not video.thumbnail_file:
        return ""
    try:
        return get_provider(video.provider).thumbnail_url(video.provider_video_id, video.thumbnail_file,
                                                         expires_in=settings.VIDEO_THUMBNAIL_TOKEN_SECONDS)
    except ProviderError:
        return ""
