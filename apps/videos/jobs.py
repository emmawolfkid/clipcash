"""Video background jobs (Phase 02D runner; provider calls run outside transactions)."""

from apps.audit.services import record_business_event
from apps.core.jobs.registry import register
from apps.core.jobs.schedules import Schedule, register_schedule

from . import services
from .models import Video


def _record_provider_failure(job):
    """on_failure: the provider job failed terminally (inside the failure transaction)."""
    video = Video.objects.filter(pk=job.payload.get("video_id")).first()
    record_business_event("video.provider.sync_failed", actor=None, target=video,
                          metadata={"job_type": job.job_type, "error_class": job.last_error_class})


@register("videos.sync_status", payload_schema={"video_id": "uuid"}, transactional=False,
          max_attempts=20, backoff_base_seconds=30, backoff_max_seconds=600,
          on_failure=_record_provider_failure, description="Synchronize a video's processing status.")
def sync_status(payload, job):
    services.sync_video(payload["video_id"])


@register("videos.delete_media", payload_schema={"video_id": "uuid"}, transactional=False,
          max_attempts=8, on_failure=_record_provider_failure,
          description="Delete an archived video's media from the provider.")
def delete_media(payload, job):
    services.delete_media(payload["video_id"])


@register("videos.sweep_unfinished", max_attempts=2, admin_retryable=False,
          description="Re-check videos stuck uploading or processing.")
def sweep_unfinished(payload, job):
    services.sweep_unfinished()


register_schedule(Schedule("videos.sweep_unfinished", "videos.sweep_unfinished", 300,
                           "Re-check videos stuck uploading or processing."))
