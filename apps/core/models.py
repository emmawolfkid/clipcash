import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
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
