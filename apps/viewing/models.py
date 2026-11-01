"""
Watch sessions (Phase 06). Use apps.viewing.services.

A session is server-owned: the server decides eligibility when it is created,
caps every credited second by server-measured elapsed time, and alone decides
the outcome at finalization. Browser-reported progress is untrusted input.

Three separate fields:
* state:   lifecycle (CREATED -> IN_PROGRESS -> FINALIZED | EXPIRED | INVALIDATED)
* outcome: verification result (VERIFIED_EVIDENCE means the server rules
           passed; it is NOT proof of human attention and NOT a reward)
* review:  staff review of suspicious sessions

Only bounded evidence counters are stored, never raw telemetry. PostgreSQL
guards (migration 0002): identity columns are immutable, transitions are
validated, evidence is frozen once the session is closed, no deletes.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.core.models import BaseModel

OPEN_STATES = ("CREATED", "IN_PROGRESS")


class WatchSession(BaseModel):
    class State(models.TextChoices):
        CREATED = "CREATED", _("Ready to play")
        IN_PROGRESS = "IN_PROGRESS", _("Watching")
        FINALIZED = "FINALIZED", _("Finished")
        EXPIRED = "EXPIRED", _("Expired")
        INVALIDATED = "INVALIDATED", _("Stopped")

    class Outcome(models.TextChoices):
        PENDING = "PENDING", _("Not finished")
        VERIFIED_EVIDENCE = "VERIFIED_EVIDENCE", _("View verified")
        INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE", _("Not enough watched")
        SUSPICIOUS_REVIEW = "SUSPICIOUS_REVIEW", _("Under review")
        REJECTED = "REJECTED", _("Not accepted")
        EXPIRED = "EXPIRED", _("Expired before playing")

    class Review(models.TextChoices):
        NONE = "NONE", _("Not required")
        PENDING = "PENDING", _("Awaiting review")
        CONFIRMED = "CONFIRMED", _("Confirmed by staff")
        REJECTED = "REJECTED", _("Rejected by staff")

    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="watch_sessions")
    campaign = models.ForeignKey("advertising.Campaign", on_delete=models.PROTECT, related_name="watch_sessions")
    campaign_revision = models.PositiveIntegerField(_("approved campaign revision"))
    video = models.ForeignKey("videos.Video", on_delete=models.PROTECT, related_name="watch_sessions")

    state = models.CharField(_("state"), max_length=12, choices=State.choices, default=State.CREATED)
    outcome = models.CharField(_("outcome"), max_length=24, choices=Outcome.choices, default=Outcome.PENDING)
    reasons = models.JSONField(_("reason codes"), default=list, blank=True)
    flags = models.JSONField(_("fraud flags"), default=list, blank=True)
    review_status = models.CharField(_("review"), max_length=10, choices=Review.choices, default=Review.NONE)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    reviewed_at = models.DateTimeField(_("reviewed"), null=True, blank=True)
    review_reason = models.CharField(_("review reason"), max_length=300, blank=True)
    # Counts against the campaign's views_per_member (1..limit); unique per member and campaign.
    qualifying_slot = models.PositiveSmallIntegerField(_("qualifying view number"), null=True, blank=True)

    start_deadline = models.DateTimeField(_("must start by"))
    expires_at = models.DateTimeField(_("expires"))
    started_at = models.DateTimeField(_("started"), null=True, blank=True)
    last_event_at = models.DateTimeField(_("last evidence"), null=True, blank=True)
    finalized_at = models.DateTimeField(_("closed"), null=True, blank=True)

    # Bounded evidence summary (milliseconds and counters; no raw events).
    video_duration_ms = models.PositiveIntegerField(_("video length (ms)"), null=True, blank=True)
    required_ms = models.PositiveIntegerField(_("required watch (ms)"), null=True, blank=True)
    credited_ms = models.PositiveIntegerField(_("plausible watch time (ms)"), default=0)
    last_sequence = models.PositiveIntegerField(default=0)
    last_position_ms = models.PositiveIntegerField(default=0)
    # Furthest point reached by continuous, credited playback from the start (no seeks).
    coverage_ms = models.PositiveIntegerField(_("continuous coverage (ms)"), default=0)
    last_state_playing = models.BooleanField(default=False)
    event_count = models.PositiveIntegerField(default=0)
    rejected_event_count = models.PositiveIntegerField(default=0)
    duplicate_event_count = models.PositiveIntegerField(default=0)
    out_of_order_count = models.PositiveIntegerField(default=0)
    seek_count = models.PositiveIntegerField(default=0)
    seek_forward_ms = models.PositiveIntegerField(default=0)
    speed_anomaly_count = models.PositiveIntegerField(default=0)
    gap_count = models.PositiveIntegerField(default=0)
    rate_rejection_count = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = _("watch session")
        ordering = ["-created_at"]
        permissions = [("review_viewing", "Can review watch sessions")]
        indexes = [
            models.Index(fields=["member", "-created_at"], name="watch_member_idx"),
            models.Index(fields=["campaign", "outcome"], name="watch_campaign_idx"),
            models.Index(fields=["state", "last_event_at"], name="watch_open_idx", condition=Q(state__in=OPEN_STATES)),
            models.Index(fields=["review_status", "finalized_at"], name="watch_review_idx",
                         condition=Q(review_status="PENDING")),
        ]
        constraints = [
            # One open session per member and campaign (duplicate starts, parallel tabs).
            models.UniqueConstraint(fields=["member", "campaign"], condition=Q(state__in=OPEN_STATES),
                                    name="watch_one_open_per_campaign"),
            # A qualifying view number is used once: no view is ever counted twice.
            models.UniqueConstraint(fields=["member", "campaign", "qualifying_slot"],
                                    condition=Q(qualifying_slot__isnull=False), name="watch_slot_unique"),
            models.CheckConstraint(condition=Q(qualifying_slot__isnull=True) | Q(qualifying_slot__gte=1),
                                   name="watch_slot_positive"),
            models.CheckConstraint(condition=Q(state__in=["CREATED", "IN_PROGRESS", "FINALIZED", "EXPIRED",
                                                          "INVALIDATED"]), name="watch_state_valid"),
            models.CheckConstraint(condition=Q(outcome__in=["PENDING", "VERIFIED_EVIDENCE", "INSUFFICIENT_EVIDENCE",
                                                            "SUSPICIOUS_REVIEW", "REJECTED", "EXPIRED"]),
                                   name="watch_outcome_valid"),
            models.CheckConstraint(condition=Q(review_status__in=["NONE", "PENDING", "CONFIRMED", "REJECTED"]),
                                   name="watch_review_valid"),
            # Open sessions have no outcome; closed ones have one and a close time.
            models.CheckConstraint(condition=(Q(state__in=OPEN_STATES) & Q(outcome="PENDING")
                                              & Q(finalized_at__isnull=True))
                                   | (~Q(state__in=OPEN_STATES) & ~Q(outcome="PENDING")
                                      & Q(finalized_at__isnull=False)),
                                   name="watch_outcome_matches_state"),
            # Only verified (or reviewed-pending) sessions hold a counted slot.
            models.CheckConstraint(condition=Q(qualifying_slot__isnull=True)
                                   | Q(outcome__in=["VERIFIED_EVIDENCE", "SUSPICIOUS_REVIEW"]),
                                   name="watch_slot_requires_qualifying"),
            models.CheckConstraint(condition=~Q(outcome="VERIFIED_EVIDENCE")
                                   | (Q(state="FINALIZED") & Q(qualifying_slot__isnull=False)
                                      & Q(required_ms__isnull=False) & Q(credited_ms__gte=models.F("required_ms"))
                                      & Q(coverage_ms__gte=models.F("required_ms"))),
                                   name="watch_verified_has_evidence"),
            models.CheckConstraint(condition=~Q(outcome="SUSPICIOUS_REVIEW") | Q(review_status="PENDING"),
                                   name="watch_suspicious_pending_review"),
            models.CheckConstraint(condition=Q(review_status__in=["NONE", "PENDING"])
                                   | (Q(reviewed_by__isnull=False) & Q(reviewed_at__isnull=False)),
                                   name="watch_review_recorded"),
            models.CheckConstraint(condition=Q(expires_at__gt=models.F("start_deadline")),
                                   name="watch_expiry_after_start_deadline"),
        ]

    def __str__(self):
        return f"Watch session {str(self.pk)[:8]}"

    @property
    def is_open(self):
        return self.state in OPEN_STATES

    @property
    def counts_toward_limit(self):
        """Holds (or may hold) one of the campaign's views for this member."""
        return self.is_open or self.qualifying_slot is not None

    @property
    def progress_percent(self):
        if not self.required_ms:
            return 0
        return min(100, self.credited_ms * 100 // self.required_ms)
