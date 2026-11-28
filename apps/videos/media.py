"""
OPS-01: protected delivery of locally stored videos and thumbnails.

Every request needs (1) a signed, expiring token for this video, bound to the signed-in viewer, and
(2) access right now: staff with the video console (preview), or a member whose membership includes the
video (the same decision as playback). Bytes are streamed in 256 KB chunks with single-range HTTP Range
support (206 Partial Content, seeking); nothing loads the whole file into memory. When the hosting
supports it (VIDEO_LOCAL_SENDFILE_HEADER), the web server sends the file instead. Responses are private,
uncached by shared caches, and never reveal the storage path.
"""

import mimetypes
import re

from django.conf import settings
from django.http import Http404, HttpResponse, StreamingHttpResponse
from django.views.decorators.http import require_GET

from . import local_storage
from .models import Video
from .providers.local import ID_PATTERN, read_token

RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
CHUNK = 256 * 1024


def _refuse(status=403):
    response = HttpResponse(status=status)
    response["Cache-Control"] = "no-store"
    return response


def _video_for(request, video_id, *, thumbnail=False):
    if not ID_PATTERN.match(video_id):
        raise Http404
    video = Video.objects.filter(provider=Video.Provider.LOCAL, provider_video_id=video_id).first()
    if video is None:
        raise Http404
    max_age = max(settings.VIDEO_LOCAL_TOKEN_SECONDS, settings.VIDEO_THUMBNAIL_TOKEN_SECONDS)
    data = read_token(request.GET.get("t", ""), video_id, max_age=max_age)
    user = request.user
    if data is None or (thumbnail and not data.get("thumb")):
        local_storage.deny(user, video, "bad_token")
        return video, False
    if data.get("u") and str(getattr(user, "pk", "")) != data["u"]:
        local_storage.deny(user, video, "other_viewer")  # A link copied to another account.
        return video, False
    allowed, reason = local_storage.can_stream(user, video)
    if not allowed:
        local_storage.deny(user, video, reason)
    return video, allowed


def _file_chunks(path, start, length):
    with open(path, "rb") as handle:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            data = handle.read(min(CHUNK, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


def _private(response, content_type):
    response["Content-Type"] = content_type
    response["Cache-Control"] = "private, max-age=0, no-transform"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Disposition"] = "inline"
    return response


@require_GET
def media(request, video_id):
    video, allowed = _video_for(request, video_id)
    if not allowed:
        return _refuse()
    path = local_storage.provider().media_path(video_id)
    if not path.is_file():
        raise Http404
    size = path.stat().st_size
    header = settings.VIDEO_LOCAL_SENDFILE_HEADER
    if header:  # The web server streams (and handles ranges); Django only decided access.
        response = HttpResponse()
        response[header] = (settings.VIDEO_LOCAL_SENDFILE_PREFIX.rstrip("/") + "/media/" + path.name
                            if settings.VIDEO_LOCAL_SENDFILE_PREFIX else str(path))
        return _private(response, "video/mp4")
    start, end, status = 0, size - 1, 200
    requested = request.headers.get("Range", "").strip()
    if requested:
        match = RANGE.match(requested)
        if not match or (not match.group(1) and not match.group(2)):
            response = _refuse(416)
            response["Content-Range"] = f"bytes */{size}"
            return response
        first, last = match.group(1), match.group(2)
        if first:
            start = int(first)
            end = min(int(last), size - 1) if last else size - 1
        else:  # Suffix range: the last N bytes.
            start = max(0, size - int(last))
        if start > end or start >= size:
            response = _refuse(416)
            response["Content-Range"] = f"bytes */{size}"
            return response
        status = 206
    length = end - start + 1
    response = StreamingHttpResponse(_file_chunks(path, start, length), status=status)
    response["Content-Length"] = str(length)
    response["Accept-Ranges"] = "bytes"
    if status == 206:
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
    return _private(response, "video/mp4")


@require_GET
def thumbnail(request, video_id):
    video, allowed = _video_for(request, video_id, thumbnail=True)
    if not allowed or not video.thumbnail_file:
        return _refuse() if not allowed else _refuse(404)
    path = local_storage.provider().thumbnail_path(video_id, video.thumbnail_file)
    if not path.is_file():
        raise Http404
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    response = StreamingHttpResponse(_file_chunks(path, 0, path.stat().st_size))
    response["Content-Length"] = str(path.stat().st_size)
    return _private(response, content_type)
