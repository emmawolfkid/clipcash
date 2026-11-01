"""
Bunny Stream provider (verified against bunny.net documentation).

Credentials (never sent to browsers):
* BUNNY_STREAM_API_KEY: library API key, sent as the ``AccessKey`` header to
  https://video.bunnycdn.com, and used server-side to sign TUS uploads.
* BUNNY_STREAM_TOKEN_SECURITY_KEY: the library's *token authentication key*
  (a different value). Signs embed URLs and CDN URLs.

Signatures:
* TUS upload:  SHA256_HEX(library_id + api_key + expires + video_id)
* Embed view:  SHA256_HEX(token_key + video_id + expires) on
               https://player.mediadelivery.net/embed/{library}/{video}
* CDN (thumbnails; advanced token):
               "HS256-" + base64url(HMAC_SHA256(token_key, path + expires))

Media protection: signing the embed URL protects the player page. HLS
playlists and segments are protected only when CDN token authentication is
enabled on the library (Bunny's player then handles segment tokens); see
docs/architecture/videos.md.
"""

import base64
import hashlib
import hmac
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .base import (
    Playback,
    ProviderNotConfigured,
    ProviderPermanentError,
    ProviderTemporaryError,
    ProviderVideoInfo,
    UploadAuthorization,
)

VIDEO_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
THUMBNAIL_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

# Bunny VideoModelStatus -> (processing state, error)
STATUS_MAP = {
    0: ("UPLOADING", ""),        # Created, waiting for the upload
    1: ("PROCESSING", ""),       # Uploaded
    2: ("PROCESSING", ""),       # Processing
    3: ("PROCESSING", ""),       # Transcoding
    4: ("READY", ""),            # Finished
    5: ("FAILED", "Bunny reported an encoding error."),
    6: ("FAILED", "Bunny reported that the upload failed."),
    7: ("PROCESSING", ""),       # JitSegmenting
    8: ("READY", ""),            # JitPlaylistsCreated (playable)
}


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would forward the AccessKey header to the
    redirect target. A 3xx response surfaces as an HTTPError instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# All authenticated Bunny API calls go through this opener (no redirects).
_OPENER = urllib.request.build_opener(_RejectRedirects)


def _sha256_hex(text):
    return hashlib.sha256(text.encode()).hexdigest()


class BunnyStreamProvider:
    code = "BUNNY"

    def __init__(self, *, library_id, api_key, cdn_hostname, token_key, api_base, embed_base, timeout):
        missing = [name for name, value in (("BUNNY_STREAM_LIBRARY_ID", library_id),
                                            ("BUNNY_STREAM_API_KEY", api_key),
                                            ("BUNNY_STREAM_CDN_HOSTNAME", cdn_hostname),
                                            ("BUNNY_STREAM_TOKEN_SECURITY_KEY", token_key)) if not value]
        if missing:
            raise ProviderNotConfigured("Bunny Stream is not configured: " + ", ".join(missing))
        if not str(library_id).isdigit():
            raise ProviderNotConfigured("BUNNY_STREAM_LIBRARY_ID must be numeric.")
        self.library_id = str(library_id)
        self._api_key = api_key
        self._token_key = token_key
        self.cdn_hostname = cdn_hostname.strip().rstrip("/").removeprefix("https://")
        self.api_base = api_base.rstrip("/")
        self.embed_base = embed_base.rstrip("/")
        self.timeout = timeout

    def __repr__(self):  # Never show credentials in logs or tracebacks.
        return f"<BunnyStreamProvider library={self.library_id}>"

    # --- HTTP -----------------------------------------------------------------

    def _request(self, method, path, body=None):
        url = f"{self.api_base}/library/{self.library_id}{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={
            "AccessKey": self._api_key, "Accept": "application/json", "Content-Type": "application/json",
        })
        try:
            with _OPENER.open(request, timeout=self.timeout) as response:  # noqa: S310 - fixed https host
                raw = response.read()
        except urllib.error.HTTPError as error:
            if error.code in (408, 425, 429) or error.code >= 500:
                raise ProviderTemporaryError(f"Bunny Stream returned HTTP {error.code}.") from None
            raise ProviderPermanentError(f"Bunny Stream rejected the request (HTTP {error.code}).") from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError):
            raise ProviderTemporaryError("Bunny Stream could not be reached.") from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise ProviderTemporaryError("Bunny Stream returned an unreadable response.") from None

    @staticmethod
    def _check_id(video_id):
        if not VIDEO_ID_RE.match(str(video_id or "")):
            raise ProviderPermanentError("Invalid Bunny video id.")
        return video_id

    # --- Operations -----------------------------------------------------------------

    def create_video(self, title):
        data = self._request("POST", "/videos", {"title": str(title)[:150]})
        guid = str(data.get("guid", ""))
        if not VIDEO_ID_RE.match(guid):
            raise ProviderTemporaryError("Bunny Stream did not return a video id.")
        return guid

    def get_video(self, video_id):
        data = self._request("GET", f"/videos/{self._check_id(video_id)}")
        code = data.get("status")
        processing, error = STATUS_MAP.get(code, ("PROCESSING", ""))
        length = data.get("length")
        thumbnail = str(data.get("thumbnailFileName") or "")
        return ProviderVideoInfo(
            processing=processing,
            status_code=code if isinstance(code, int) else None,
            duration_seconds=int(length) if isinstance(length, (int, float)) and length > 0 else None,
            thumbnail_file=thumbnail if THUMBNAIL_RE.match(thumbnail) else "",
            error=error,
        )

    def delete_video(self, video_id):
        try:
            self._request("DELETE", f"/videos/{self._check_id(video_id)}")
        except ProviderPermanentError as error:
            if "HTTP 404" not in str(error):  # Already gone is success (idempotent).
                raise

    def upload_authorization(self, video_id, *, expires_in, title, filetype):
        self._check_id(video_id)
        expires = int(time.time()) + int(expires_in)
        signature = _sha256_hex(f"{self.library_id}{self._api_key}{expires}{video_id}")
        return UploadAuthorization(
            endpoint="https://video.bunnycdn.com/tusupload",
            headers={"AuthorizationSignature": signature, "AuthorizationExpire": str(expires),
                     "VideoId": video_id, "LibraryId": self.library_id},
            metadata={"filetype": filetype, "title": str(title)[:150]},
            expires_at=datetime.fromtimestamp(expires, tz=timezone.utc),
        )

    def playback(self, video_id, *, expires_in):
        self._check_id(video_id)
        expires = int(time.time()) + int(expires_in)
        token = _sha256_hex(f"{self._token_key}{video_id}{expires}")
        query = urllib.parse.urlencode({"token": token, "expires": expires, "autoplay": "false",
                                        "preload": "true", "responsive": "true"})
        return Playback(kind="iframe", url=f"{self.embed_base}/{self.library_id}/{video_id}?{query}",
                        expires_at=datetime.fromtimestamp(expires, tz=timezone.utc))

    def sign_cdn_path(self, path, *, expires_in):
        """Advanced CDN token for one file (query-string form)."""
        expires = int(time.time()) + int(expires_in)
        digest = hmac.new(self._token_key.encode(), f"{path}{expires}".encode(), hashlib.sha256).digest()
        token = "HS256-" + base64.urlsafe_b64encode(digest).decode().rstrip("=")
        return f"https://{self.cdn_hostname}{path}?{urllib.parse.urlencode({'token': token, 'expires': expires})}"

    def thumbnail_url(self, video_id, thumbnail_file, *, expires_in):
        if not thumbnail_file or not VIDEO_ID_RE.match(str(video_id)) or not THUMBNAIL_RE.match(thumbnail_file):
            return ""
        return self.sign_cdn_path(f"/{video_id}/{thumbnail_file}", expires_in=expires_in)
