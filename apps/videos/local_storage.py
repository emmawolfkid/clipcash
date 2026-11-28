"""
OPS-01: local video storage services (upload, access, file deletion, local copies, usage).

Uploads arrive through Django's normal multipart handling, which spools anything larger than
FILE_UPLOAD_MAX_MEMORY_SIZE to a temporary file, so a video is never held in Python memory. The file is
checked (MP4 header, size, the storage limit), moved under VIDEO_LOCAL_ROOT with a random name, and only
then recorded on the video (READY, with the duration read from the MP4 header or, if the header has none,
the duration the uploader's browser measured). Video records are never deleted (database guard): an
archived local video can have its FILE deleted, and the record and viewing history stay.
"""

import os
import shutil
from pathlib import Path

from django.conf import settings
from django.core.files.move import file_move_safe
from django.db import transaction
from django.utils.text import slugify
from django.utils.translation import gettext as _

from apps.audit.services import DENIED, record_business_event, record_security_event

from .models import Video
from .providers import get_provider
from .providers.local import looks_like_mp4, mp4_duration_seconds

P = Video.Processing
PUB = Video.Publication
CHUNK = 256 * 1024
MAX_DURATION = 6 * 3600


class LocalStorageError(Exception):
    def __init__(self, message, *, code="invalid"):
        super().__init__(message)
        self.code = code
        self.messages = [str(message)]


def provider():
    return get_provider("LOCAL")


def limits():
    return {"file_bytes": settings.VIDEO_LOCAL_MAX_FILE_MB * 1024 * 1024,
            "total_bytes": settings.VIDEO_LOCAL_TOTAL_MB * 1024 * 1024}


def storage_summary():
    """Usage figures for the Video storage page (bytes)."""
    store = provider()
    used = store.usage_bytes()
    info = {"used": used, **limits(), "free_disk": None, "videos": []}
    info["remaining"] = max(0, info["total_bytes"] - used)
    try:
        root = store.root if store.root.exists() else store.root.parent
        info["free_disk"] = shutil.disk_usage(root).free
    except OSError:
        pass
    for video in Video.objects.filter(provider=Video.Provider.LOCAL).exclude(provider_video_id="").order_by("-created_at")[:200]:
        try:
            size = store.media_path(video.provider_video_id).stat().st_size
        except (OSError, Exception):  # Missing file or invalid id: reported as 0 / missing.
            size = None
        info["videos"].append({"video": video, "size": size})
    return info


def _check_upload(actor, video):
    from . import services

    services._require(services.can_manage_videos, actor, "video.local_upload", video)
    if video.provider != Video.Provider.LOCAL:
        raise LocalStorageError(_("This video is not stored locally. Create a local copy to upload a file."),
                                code="not_local")
    if video.publication_status == PUB.ARCHIVED:
        raise LocalStorageError(_("Archived videos cannot receive uploads."), code="archived")
    if video.provider_video_id:
        raise LocalStorageError(_("This video already has its file."), code="has_media")


def _read_head(uploaded, count=16):
    uploaded.seek(0)
    head = uploaded.read(count)
    uploaded.seek(0)
    return head


def _thumbnail_extension(head):
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return None


def _write(uploaded, destination):
    """Move a spooled upload into place (same filesystem: rename) or copy it in chunks."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = getattr(uploaded, "temporary_file_path", None)
    if temporary is not None:
        file_move_safe(temporary(), str(destination), allow_overwrite=False)  # Rename, or chunked copy.
    else:
        with open(destination, "wb") as target:
            for chunk in uploaded.chunks(CHUNK):
                target.write(chunk)
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass


def store_upload(actor, video, media, *, thumbnail=None, reported_duration=None):
    """Validate and store one MP4 (and optional JPEG/PNG thumbnail); the video becomes READY."""
    video = Video.objects.get(pk=video.pk)
    _check_upload(actor, video)
    size = getattr(media, "size", 0) or 0
    caps = limits()
    if size <= 0:
        raise LocalStorageError(_("Choose a video file."), code="empty")
    if size > caps["file_bytes"]:
        raise LocalStorageError(_("The file is larger than %(mb)s MB. Compress it and try again.")
                                % {"mb": settings.VIDEO_LOCAL_MAX_FILE_MB}, code="file_size")
    store = provider()
    if store.usage_bytes() + size > caps["total_bytes"]:
        raise LocalStorageError(_("There is not enough video storage left. Archive and delete old video files, "
                                  "or ask for a larger storage limit."), code="storage_full")
    if not looks_like_mp4(_read_head(media)):
        raise LocalStorageError(_("Upload an MP4 video (H.264). Other formats cannot play in every browser."),
                                code="file_type")
    thumb_ext = None
    if thumbnail is not None and getattr(thumbnail, "size", 0):
        if thumbnail.size > settings.VIDEO_LOCAL_THUMBNAIL_MAX_BYTES:
            raise LocalStorageError(_("The thumbnail must be smaller than 2 MB."), code="thumbnail_size")
        thumb_ext = _thumbnail_extension(_read_head(thumbnail))
        if thumb_ext is None:
            raise LocalStorageError(_("The thumbnail must be a JPEG or PNG image."), code="thumbnail_type")

    video_id = store.new_id()
    media_path = store.media_path(video_id)
    thumb_name = f"{video_id}.{thumb_ext}" if thumb_ext else ""
    written = []
    try:
        _write(media, media_path)
        written.append(media_path)
        if thumb_ext:
            thumb_path = store.thumbnail_path(video_id, thumb_name)
            _write(thumbnail, thumb_path)
            written.append(thumb_path)
        duration = mp4_duration_seconds(media_path)
        if not duration and reported_duration:
            try:
                value = round(float(reported_duration))
                duration = value if 1 <= value <= MAX_DURATION else None
            except (TypeError, ValueError):
                duration = None
        with transaction.atomic():
            locked = Video.objects.select_for_update().get(pk=video.pk)
            _check_upload(actor, locked)
            locked.provider_video_id = video_id
            locked.thumbnail_file = thumb_name
            locked.duration_seconds = duration or locked.duration_seconds
            locked.processing_status = P.READY
            locked.processing_error = ""
            locked.save(update_fields=["provider_video_id", "thumbnail_file", "duration_seconds",
                                       "processing_status", "processing_error", "updated_at"])
            record_business_event("video.local.stored", actor=actor, target=locked, metadata={
                "size_mb": max(1, size // (1024 * 1024)), "duration_seconds": locked.duration_seconds or 0,
                "thumbnail": bool(thumb_name)})
        return locked
    except Exception:
        for path in written:  # Never leave an orphan file behind a failed upload.
            Path(path).unlink(missing_ok=True)
        raise


def delete_file(actor, video):
    """Super Administrator: delete an ARCHIVED local video's file (the record and history stay)."""
    from apps.memberships.services import is_super_admin

    from . import services

    services._require(is_super_admin, actor, "video.delete_media", video)
    with transaction.atomic():
        locked = Video.objects.select_for_update().get(pk=video.pk)
        if locked.provider != Video.Provider.LOCAL or locked.publication_status != PUB.ARCHIVED \
                or not locked.provider_video_id:
            raise LocalStorageError(_("Only archived local videos can have their file deleted."), code="not_archived")
        provider().delete_video(locked.provider_video_id)
        locked.processing_status = P.FAILED
        locked.processing_error = "Video file deleted from storage."
        locked.thumbnail_file = ""
        locked.save(update_fields=["processing_status", "processing_error", "thumbnail_file", "updated_at"])
        record_business_event("video.media.deleted", actor=actor, target=locked)
    return locked


def create_local_copy(actor, video):
    """A new LOCAL draft with the same details, ready for a file upload. The original video, its
    campaigns and its viewing history are not changed (its provider id cannot change: database guard)."""
    from . import services

    services._require(services.can_manage_videos, actor, "video.local_copy", video)
    if video.provider == Video.Provider.LOCAL:
        raise LocalStorageError(_("This video is already stored locally."), code="already_local")
    base = slugify(f"{video.slug}-local")[:150] or "video-local"
    slug, counter = base, 2
    while Video.objects.filter(slug=slug).exists():
        slug, counter = f"{base}-{counter}", counter + 1
    with transaction.atomic():
        copy = Video(provider=Video.Provider.LOCAL, created_by=actor, title=video.title, slug=slug,
                     description=video.description, category=video.category,
                     required_entitlement=video.required_entitlement, duration_seconds=video.duration_seconds)
        copy.full_clean()
        copy.save()
        record_business_event("video.created", actor=actor, target=copy, metadata={"provider": "LOCAL", "slug": slug})
        record_business_event("video.local.copy_created", actor=actor, target=copy, metadata={"source": str(video.pk)})
    return copy


def can_stream(user, video):
    """May ``user`` receive this local video's bytes now? Staff with the video console (preview), or
    the same decision as playback for members (published, ready, membership entitlement)."""
    from . import services

    if user is None or not getattr(user, "is_authenticated", False):
        return False, "not_authenticated"
    if services.can_use_video_console(user):
        return True, "staff_preview"
    reason, _playback = services.playback_decision(user, video)
    return reason == "ok", reason


def deny(user, video, reason):
    record_security_event("video.media.access_denied", outcome=DENIED,
                          actor=user if getattr(user, "is_authenticated", False) else None, target=video,
                          metadata={"reason": reason[:40]})
