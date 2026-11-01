"""
Video categories and videos.

Processing state (provider pipeline) is separate from publication state
(editorial). PostgreSQL enforces: a PUBLISHED video is READY and has a
publication time; an ARCHIVED video has an archive time and stays archived;
videos are never deleted (migration 0002). Use apps.videos.services.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from apps.core.models import BaseModel
from apps.memberships.entitlements import ENTITLEMENTS


class VideoCategory(BaseModel):
    name = models.CharField(_("name"), max_length=80)
    slug = models.SlugField(_("slug"), max_length=80, unique=True)
    description = models.CharField(_("description"), max_length=300, blank=True)
    display_order = models.PositiveSmallIntegerField(_("display order"), default=100)
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        verbose_name = _("video category")
        verbose_name_plural = _("video categories")
        ordering = ["display_order", "name"]
        indexes = [models.Index(fields=["is_active", "display_order"], name="video_category_active_idx")]
        constraints = [
            models.UniqueConstraint(Lower("name"), name="video_category_name_ci_unique",
                                    violation_error_message=_("A category with this name already exists.")),
            models.CheckConstraint(condition=~Q(name=""), name="video_category_name_not_blank"),
        ]

    def __str__(self):
        return self.name


class Video(BaseModel):
    class Provider(models.TextChoices):
        BUNNY = "BUNNY", _("Bunny Stream")
        DEMO = "DEMO", _("Demo sample (no real media)")

    class Processing(models.TextChoices):
        PENDING = "PENDING", _("Waiting for upload")
        UPLOADING = "UPLOADING", _("Uploading")
        PROCESSING = "PROCESSING", _("Processing")
        READY = "READY", _("Ready")
        FAILED = "FAILED", _("Failed")

    class Publication(models.TextChoices):
        DRAFT = "DRAFT", _("Draft")
        PUBLISHED = "PUBLISHED", _("Published")
        ARCHIVED = "ARCHIVED", _("Archived")

    title = models.CharField(_("title"), max_length=150)
    slug = models.SlugField(_("slug"), max_length=160, unique=True)
    description = models.TextField(_("description"), max_length=3000, blank=True)
    category = models.ForeignKey(VideoCategory, verbose_name=_("category"), null=True, blank=True,
                                 on_delete=models.PROTECT, related_name="videos")
    required_entitlement = models.CharField(
        _("required entitlement"), max_length=40, default="videos.standard",
        choices=[(code, label) for code, label in ENTITLEMENTS.items() if code.startswith("videos.")],
        help_text=_("Membership entitlement needed to play this video."),
    )

    provider = models.CharField(_("provider"), max_length=10, choices=Provider.choices)
    provider_video_id = models.CharField(_("provider video id"), max_length=64, blank=True)
    provider_library_id = models.CharField(_("provider library id"), max_length=32, blank=True)
    provider_status_code = models.SmallIntegerField(_("provider status code"), null=True, blank=True)
    remote_creation_started_at = models.DateTimeField(null=True, blank=True, editable=False)

    duration_seconds = models.PositiveIntegerField(_("duration (seconds)"), null=True, blank=True)
    thumbnail_file = models.CharField(_("thumbnail file"), max_length=100, blank=True)

    processing_status = models.CharField(_("processing"), max_length=12, choices=Processing.choices,
                                         default=Processing.PENDING)
    processing_error = models.CharField(_("processing error"), max_length=200, blank=True)
    last_synced_at = models.DateTimeField(_("last synchronized"), null=True, blank=True)

    publication_status = models.CharField(_("publication"), max_length=10, choices=Publication.choices,
                                          default=Publication.DRAFT)
    published_at = models.DateTimeField(_("published at"), null=True, blank=True)
    archived_at = models.DateTimeField(_("archived at"), null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name=_("created by"), null=True,
                                   blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        verbose_name = _("video")
        verbose_name_plural = _("videos")
        ordering = ["-published_at", "-created_at"]
        permissions = [
            ("manage_videos", "Can create, edit and upload videos"),
            ("publish_videos", "Can publish, unpublish and archive videos"),
            ("manage_categories", "Can manage video categories"),
        ]
        indexes = [
            models.Index(fields=["publication_status", "processing_status", "-published_at"],
                         name="video_library_idx"),
            models.Index(fields=["category", "-published_at"], name="video_category_idx"),
            models.Index(fields=["processing_status"], name="video_processing_idx",
                         condition=Q(processing_status__in=["UPLOADING", "PROCESSING"])),
        ]
        constraints = [
            models.UniqueConstraint(fields=["provider", "provider_video_id"], condition=~Q(provider_video_id=""),
                                    name="video_provider_id_unique"),
            models.CheckConstraint(condition=~Q(title=""), name="video_title_not_blank"),
            models.CheckConstraint(
                condition=~Q(publication_status="PUBLISHED")
                | Q(processing_status="READY", published_at__isnull=False) & ~Q(provider_video_id=""),
                name="video_published_requires_ready"),
            models.CheckConstraint(condition=~Q(publication_status="ARCHIVED") | Q(archived_at__isnull=False),
                                   name="video_archived_has_time"),
            models.CheckConstraint(condition=Q(provider__in=["BUNNY", "DEMO"]), name="video_provider_valid"),
        ]

    def __str__(self):
        return self.title

    @property
    def is_demo(self):
        return self.provider == self.Provider.DEMO

    @property
    def is_playable(self):
        return (self.publication_status == self.Publication.PUBLISHED
                and self.processing_status == self.Processing.READY)

    @property
    def duration_display(self):
        if not self.duration_seconds:
            return ""
        minutes, seconds = divmod(self.duration_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
