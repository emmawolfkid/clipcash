import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.phone_numbers import normalize_phone_number, validate_e164

from .money import DEFAULT_CURRENCY, SUPPORTED_CURRENCIES
from .validators import validate_https_url, validate_language, validate_timezone

# --- Shared abstract models -------------------------------------------------------


class UUIDModel(models.Model):
    """Random UUID primary key: not guessable or enumerable in URLs."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        abstract = True


class BaseModel(UUIDModel, TimeStampedModel):
    """Default base for new ClipCash models."""

    class Meta:
        abstract = True


# --- Platform configuration ---------------------------------------------------------

SITE_CONFIGURATION_ID = 1


class SiteConfiguration(TimeStampedModel):
    """
    Database-editable business settings: a single row (id = 1).

    Environment-owned settings (secrets, database credentials, hosts, security
    flags, supported languages) are deliberately absent: they are read from the
    environment in config/settings.py and cannot be changed through the web.
    Financial features (real-money rewards, live payments, withdrawals) have no
    field here at all; see apps.core.features.
    """

    id = models.PositiveSmallIntegerField(
        primary_key=True, default=SITE_CONFIGURATION_ID, editable=False
    )

    # Identity and branding
    platform_name = models.CharField(_("platform name"), max_length=100, default="ClipCash")
    platform_description = models.TextField(_("platform description"), max_length=500, blank=True)
    company_name = models.CharField(_("company name"), max_length=150, blank=True)
    company_address = models.TextField(_("company address"), max_length=500, blank=True)
    website_url = models.URLField(
        _("website"), max_length=200, blank=True, validators=[validate_https_url]
    )

    # Support contact
    support_email = models.EmailField(_("support email"), blank=True)
    support_phone = models.CharField(
        _("support phone"),
        max_length=16,
        blank=True,
        validators=[validate_e164],
        help_text=_("International format, for example +255712345678."),
    )

    # Regional defaults
    default_timezone = models.CharField(
        _("default time zone"),
        max_length=64,
        default="Africa/Dar_es_Salaam",
        validators=[validate_timezone],
    )
    default_currency = models.CharField(
        _("default currency"),
        max_length=3,
        default=DEFAULT_CURRENCY,
        choices=[(code, code) for code in SUPPORTED_CURRENCIES],
    )
    default_language = models.CharField(
        _("default language"),
        max_length=10,
        default="en",
        validators=[validate_language],
        help_text=_("Used when a visitor has not chosen a language and the browser expresses no preference."),
    )

    # Availability
    maintenance_mode = models.BooleanField(
        _("maintenance mode"),
        default=False,
        help_text=_("When on, only signed-in staff can use the site."),
    )
    maintenance_message = models.CharField(_("maintenance message"), max_length=300, blank=True)
    registration_enabled = models.BooleanField(_("public registration"), default=True)
    memberships_enabled = models.BooleanField(_("membership features"), default=False)
    videos_enabled = models.BooleanField(_("video features"), default=False)
    advertisers_enabled = models.BooleanField(_("advertiser features"), default=False)

    # Optimistic concurrency: every successful update increments this.
    version = models.PositiveIntegerField(_("version"), default=1, editable=False)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("updated by"),
        null=True,
        blank=True,
        editable=False,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = _("site configuration")
        verbose_name_plural = _("site configuration")
        constraints = [
            models.CheckConstraint(
                condition=Q(id=SITE_CONFIGURATION_ID), name="core_siteconfiguration_singleton"
            ),
            models.CheckConstraint(
                condition=Q(default_currency__in=SUPPORTED_CURRENCIES),
                name="core_siteconfiguration_currency_supported",
            ),
            models.CheckConstraint(
                condition=~Q(platform_name=""), name="core_siteconfiguration_name_not_blank"
            ),
        ]

    def __str__(self):
        return str(self._meta.verbose_name).capitalize()

    TEXT_FIELDS = (
        "platform_name",
        "platform_description",
        "company_name",
        "company_address",
        "maintenance_message",
    )

    def clean_fields(self, exclude=None):
        # Normalize before field validators run, so "0712 345 678" is accepted.
        exclude = set(exclude or ())
        errors = {}
        for name in self.TEXT_FIELDS:
            setattr(self, name, (getattr(self, name) or "").strip())
        try:
            self.support_phone = normalize_phone_number(self.support_phone) or ""
        except ValidationError as error:
            errors["support_phone"] = error.error_list
            exclude.add("support_phone")
        try:
            super().clean_fields(exclude=exclude)
        except ValidationError as error:
            errors = error.update_error_dict(errors)
        if errors:
            raise ValidationError(errors)


# --- Background jobs ------------------------------------------------------------


class Job(BaseModel):
    """
    One unit of background work. Rows are claimed with SELECT ... FOR UPDATE
    SKIP LOCKED; execution is at-least-once, so handlers must be idempotent.
    Only types registered in apps.core.jobs.registry can run; the payload holds
    validated identifiers, never code or secrets.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        RUNNING = "RUNNING", _("Running")
        SUCCEEDED = "SUCCEEDED", _("Succeeded")
        RETRY_WAIT = "RETRY_WAIT", _("Waiting to retry")
        FAILED = "FAILED", _("Failed")
        CANCELLED = "CANCELLED", _("Cancelled")

    class ErrorClass(models.TextChoices):
        TRANSIENT = "TRANSIENT", _("Transient")
        PERMANENT = "PERMANENT", _("Permanent")
        UNEXPECTED = "UNEXPECTED", _("Unexpected")
        LEASE_EXPIRED = "LEASE_EXPIRED", _("Lease expired")
        INTERRUPTED = "INTERRUPTED", _("Interrupted")

    RUNNABLE = (Status.PENDING, Status.RETRY_WAIT)

    job_type = models.CharField(_("job type"), max_length=100)
    payload = models.JSONField(_("payload"), default=dict, blank=True)
    status = models.CharField(_("status"), max_length=12, choices=Status.choices, default=Status.PENDING)
    priority = models.PositiveSmallIntegerField(_("priority"), default=100, help_text=_("Lower runs first."))
    run_after = models.DateTimeField(_("run after"), default=timezone.now)
    attempts = models.PositiveSmallIntegerField(_("attempts"), default=0)
    max_attempts = models.PositiveSmallIntegerField(_("max attempts"), default=5)
    idempotency_key = models.CharField(_("idempotency key"), max_length=200, null=True, blank=True, unique=True)
    started_at = models.DateTimeField(_("started at"), null=True, blank=True)
    finished_at = models.DateTimeField(_("finished at"), null=True, blank=True)
    lease_expires_at = models.DateTimeField(_("lease expires at"), null=True, blank=True)
    worker_id = models.CharField(_("worker"), max_length=100, blank=True)
    last_error_class = models.CharField(_("error class"), max_length=15, choices=ErrorClass.choices, blank=True)
    last_error = models.CharField(_("error summary"), max_length=300, blank=True)
    request_id = models.CharField(_("request id"), max_length=64, blank=True)

    class Meta:
        verbose_name = _("background job")
        verbose_name_plural = _("background jobs")
        ordering = ["-created_at"]
        permissions = [
            ("view_operations", "Can view background jobs, workers and email delivery"),
            ("retry_job", "Can retry failed background jobs"),
            ("cancel_job", "Can cancel background jobs"),
            ("manage_schedules", "Can enable or disable scheduled tasks"),
        ]
        indexes = [
            models.Index(fields=["priority", "run_after", "created_at"], name="job_runnable_idx",
                         condition=Q(status__in=["PENDING", "RETRY_WAIT"])),
            models.Index(fields=["lease_expires_at"], name="job_running_lease_idx", condition=Q(status="RUNNING")),
            models.Index(fields=["status", "-created_at"], name="job_status_idx"),
            models.Index(fields=["job_type", "-created_at"], name="job_type_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["PENDING", "RUNNING", "SUCCEEDED", "RETRY_WAIT", "FAILED", "CANCELLED"]),
                name="job_status_valid"),
            models.CheckConstraint(condition=Q(max_attempts__gte=1, max_attempts__lte=25), name="job_max_attempts_range"),
            models.CheckConstraint(condition=Q(attempts__lte=models.F("max_attempts")), name="job_attempts_within_max"),
            models.CheckConstraint(condition=Q(priority__lte=1000), name="job_priority_range"),
            models.CheckConstraint(condition=~Q(status="RUNNING") | Q(lease_expires_at__isnull=False, worker_id__gt=""),
                                   name="job_running_has_lease"),
        ]

    def __str__(self):
        return f"{self.job_type} [{self.status}]"


class WorkerHeartbeat(models.Model):
    """Last-known state of each worker (one row per worker name)."""

    worker_id = models.CharField(_("worker"), max_length=100, primary_key=True)
    last_started_at = models.DateTimeField(_("last run started"), null=True, blank=True)
    last_seen_at = models.DateTimeField(_("last seen"), default=timezone.now)
    last_run_seconds = models.FloatField(_("last run seconds"), default=0)
    last_processed = models.PositiveIntegerField(_("jobs in last run"), default=0)
    last_succeeded = models.PositiveIntegerField(_("succeeded in last run"), default=0)
    last_failed = models.PositiveIntegerField(_("failed or retried in last run"), default=0)
    total_processed = models.PositiveBigIntegerField(_("total jobs"), default=0)
    cache_ok = models.BooleanField(_("cache healthy"), null=True, blank=True)
    cache_checked_at = models.DateTimeField(_("cache checked at"), null=True, blank=True)

    class Meta:
        verbose_name = _("worker heartbeat")
        verbose_name_plural = _("worker heartbeats")
        ordering = ["-last_seen_at"]

    def __str__(self):
        return self.worker_id


class ScheduledTask(models.Model):
    """Run state of a code-defined schedule (apps.core.jobs.schedules)."""

    name = models.CharField(_("name"), max_length=100, primary_key=True)
    enabled = models.BooleanField(_("enabled"), default=True)
    next_run_at = models.DateTimeField(_("next run"), default=timezone.now)
    last_enqueued_at = models.DateTimeField(_("last enqueued"), null=True, blank=True)

    class Meta:
        verbose_name = _("scheduled task")
        verbose_name_plural = _("scheduled tasks")
        ordering = ["name"]

    def __str__(self):
        return self.name


class RateLimitCounter(models.Model):
    """
    Shared, atomic rate-limit counters (apps.core.ratelimit). Django's database
    cache cannot increment atomically, so counters live in their own table and
    are incremented with INSERT ... ON CONFLICT DO UPDATE.
    """

    key = models.CharField(max_length=100, primary_key=True)
    count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["expires_at"], name="ratelimit_expires_idx")]
