import logging
import zoneinfo

from django.conf import settings
from django.db import DatabaseError, connection
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone, translation

from .services import get_site_configuration

logger = logging.getLogger("clipcash.health")


class HealthCheckMiddleware:
    """
    Answers GET/HEAD HEALTH_CHECK_PATH for the hosting platform's health probe:
    200 when this process serves requests and the database answers, otherwise
    503. Placed first, so the probe skips host validation, the HTTPS redirect,
    sessions and maintenance mode. The body reveals nothing else.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path_info != settings.HEALTH_CHECK_PATH or request.method not in ("GET", "HEAD"):
            return self.get_response(request)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            healthy = True
        except DatabaseError as exc:
            # Class name only: driver messages can name hosts and users.
            logger.error("Health check failed: database unavailable (%s).", type(exc).__name__)
            healthy = False
        response = HttpResponse("ok" if healthy else "unavailable", content_type="text/plain",
                                status=200 if healthy else 503)
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class SiteConfigurationMiddleware:
    """
    Per request: attach the site configuration, activate its time zone, apply its
    default language when the visitor expressed no preference, and enforce
    maintenance mode (only ACTIVE staff pass; sign-in stays reachable).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def _maintenance_exempt(self, request):
        paths = (
            reverse("admin:index"),
            reverse("accounts:login"),
            reverse("accounts:logout"),
            reverse("set_language"),
        )
        return request.path.startswith(paths) or request.path.startswith(settings.STATIC_URL)

    def __call__(self, request):
        config = get_site_configuration()
        request.site_configuration = config

        try:
            timezone.activate(zoneinfo.ZoneInfo(config.default_timezone))
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            timezone.deactivate()

        if (
            settings.LANGUAGE_COOKIE_NAME not in request.COOKIES
            and "HTTP_ACCEPT_LANGUAGE" not in request.META
            and config.default_language in dict(settings.LANGUAGES)
        ):
            translation.activate(config.default_language)
            request.LANGUAGE_CODE = config.default_language

        if (
            config.maintenance_mode
            and not (request.user.is_authenticated and request.user.is_staff and request.user.is_active)
            and not self._maintenance_exempt(request)
        ):
            response = render(request, "core/maintenance.html", {"config": config}, status=503)
            response["Retry-After"] = "600"
            return response

        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()
