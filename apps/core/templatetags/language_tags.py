from pathlib import Path

from django import template
from django.conf import settings
from django.utils import translation

register = template.Library()


def _has_catalogue(code):
    return any((Path(base) / code / "LC_MESSAGES" / "django.mo").exists() for base in settings.LOCALE_PATHS)


@register.simple_tag
def switchable_languages():
    """Languages worth offering: the default, the one in use (so a visitor can always switch
    back) and any with a compiled catalogue. Offering Swahili with no translation would leave
    English text on a page declaring lang="sw". Adding a catalogue brings it back automatically."""
    current = translation.get_language()
    return [(code, name) for code, name in settings.LANGUAGES
            if code in (settings.LANGUAGE_CODE, current) or _has_catalogue(code)]
