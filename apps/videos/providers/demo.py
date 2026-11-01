"""
Demo provider: lets the library be shown without Bunny Stream.

It never contacts any external service and never pretends an upload or
processing happened: demo videos are labelled "Demo sample", have no
uploads, and play either a sample file the owner configured
(VIDEO_DEMO_SAMPLE_URL, e.g. a self-made or properly licensed clip in
static/) or a labelled placeholder. Disabled in production unless an
isolated demo deployment sets VIDEO_DEMO_ALLOWED.
"""

import uuid

from .base import Playback, ProviderPermanentError, ProviderVideoInfo


class DemoProvider:
    code = "DEMO"

    def __init__(self, *, sample_url=""):
        self.sample_url = sample_url

    def __repr__(self):
        return "<DemoProvider>"

    def create_video(self, title):
        return f"demo-{uuid.uuid4().hex}"

    def get_video(self, video_id):
        # Demo records have no media pipeline: they are "ready" to show the UI.
        return ProviderVideoInfo(processing="READY")

    def delete_video(self, video_id):
        return None

    def upload_authorization(self, video_id, **kwargs):
        raise ProviderPermanentError("Demo mode has no uploads. Configure Bunny Stream to upload videos.")

    def playback(self, video_id, *, expires_in):
        if self.sample_url:
            return Playback(kind="video", url=self.sample_url)
        return Playback(kind="placeholder")

    def thumbnail_url(self, video_id, thumbnail_file, *, expires_in):
        return ""
