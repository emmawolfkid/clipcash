"""
Site configuration access.

Reads are cached briefly and never write. If the row is missing or the database
is unavailable, an unsaved instance with the model defaults is returned, so
features fall back to their documented defaults (financial features are
locked off regardless; see apps.core.features).

Updates go through update_site_configuration(), which checks authorization,
locks the row, rejects stale versions, validates and records who changed what.
"""

import logging

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.utils.translation import gettext_lazy as _

from .models import SITE_CONFIGURATION_ID, SiteConfiguration

security_log = logging.getLogger("clipcash.security")

CACHE_KEY = "core:site-configuration"
CACHE_SECONDS = 60

# Fields a Super Administrator may change. Anything else is rejected.
EDITABLE_FIELDS = frozenset(
    {
        "platform_name",
        "platform_description",
        "company_name",
        "company_address",
        "website_url",
        "support_email",
        "support_phone",
        "default_timezone",
        "default_currency",
        "default_language",
        "maintenance_mode",
        "maintenance_message",
        "registration_enabled",
        "memberships_enabled",
        "videos_enabled",
        "advertisers_enabled",
    }
)


class StaleConfigurationError(ValidationError):
    """The configuration changed after the editor loaded it."""


def default_site_configuration():
    return SiteConfiguration(id=SITE_CONFIGURATION_ID)


def get_site_configuration():
    try:
        config = cache.get(CACHE_KEY)
    except Exception:  # noqa: BLE001 - cache outages must not break pages
        config = None
    if config is not None:
        return config
    try:
        config = SiteConfiguration.objects.filter(id=SITE_CONFIGURATION_ID).first()
    except DatabaseError:
        security_log.exception("site_configuration_unavailable")
        return default_site_configuration()
    if config is None:
        security_log.error("site_configuration_missing; using defaults")
        return default_site_configuration()
    try:
        cache.set(CACHE_KEY, config, CACHE_SECONDS)
    except Exception:  # noqa: BLE001
        pass
    return config


def invalidate_site_configuration_cache():
    try:
        cache.delete(CACHE_KEY)
    except Exception:  # noqa: BLE001
        security_log.exception("site_configuration_cache_invalidation_failed")


def can_change_site_configuration(user):
    """Only ACTIVE Super Administrators may change business configuration."""
    return bool(
        user is not None
        and user.is_authenticated
        and user.is_active
        and user.is_superuser
        and getattr(user, "role", None) == user.Role.SUPER_ADMIN
    )


def update_site_configuration(actor, *, expected_version, changes):
    """
    Apply ``changes`` (field -> value) on behalf of ``actor``.

    Raises PermissionError if the actor is not allowed, ValidationError for
    unknown fields or invalid values, and StaleConfigurationError when
    ``expected_version`` is not the current version (someone else saved first).
    """
    if not can_change_site_configuration(actor):
        raise PermissionError("Only active Super Administrators can change site configuration.")

    unknown = set(changes) - EDITABLE_FIELDS
    if unknown:
        raise ValidationError(
            _("These settings cannot be changed here: %(fields)s.")
            % {"fields": ", ".join(sorted(unknown))},
            code="not_editable",
        )

    with transaction.atomic():
        config, _created = SiteConfiguration.objects.select_for_update().get_or_create(
            id=SITE_CONFIGURATION_ID
        )
        if config.version != expected_version:
            raise StaleConfigurationError(
                _("The configuration was changed by someone else. Reload the page and try again."),
                code="stale",
            )

        before = {name: getattr(config, name) for name in changes}
        for name, value in changes.items():
            setattr(config, name, value)
        config.full_clean()

        changed = sorted(name for name in changes if getattr(config, name) != before[name])
        if not changed:
            return config
        config.version += 1
        config.updated_by = actor
        config.save()

        # Drop the cache now (this process) and again after commit (others).
        invalidate_site_configuration_cache()
        transaction.on_commit(invalidate_site_configuration_cache)

    security_log.warning(
        "site_configuration_changed actor=%s version=%s fields=%s",
        actor.pk,
        config.version,
        ",".join(changed),
    )
    return config
