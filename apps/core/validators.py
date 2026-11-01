import zoneinfo
from functools import lru_cache

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.utils.translation import gettext_lazy as _


@lru_cache(maxsize=1)
def _available_timezones():
    return frozenset(zoneinfo.available_timezones())


def validate_timezone(value):
    if value not in _available_timezones():
        raise ValidationError(_("Enter a valid IANA time zone, for example Africa/Dar_es_Salaam."),
                              code="invalid_timezone")


def validate_language(value):
    if value not in dict(settings.LANGUAGES):
        raise ValidationError(_("Choose one of the supported languages."), code="invalid_language")


validate_https_url = URLValidator(schemes=["https"], message=_("Enter a valid https:// address."))
