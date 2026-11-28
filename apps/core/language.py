"""ACC-01: one language preference for the whole site.

Before ACC-01 three places chose the language independently: Django's LocaleMiddleware (cookie, then
the browser's Accept-Language), the site configuration default (only when the browser sent nothing)
and the OPS-01 console default (only on /manage/ pages). With no explicit choice, the language
therefore depended on the page: Swahili in the Operations Console, the browser's language on the
sign-in page, Django administration and member pages. Nothing was saved with the account, so signing
in never restored a person's choice.

Now one resolver decides, in this order:

1. Signed in with a saved choice: the account's ``preferred_language``.
2. An explicit choice made on this device: the language cookie set by the EN/SW switch.
3. No choice yet, staff (or the console and its sign-in page): ``CONSOLE_DEFAULT_LANGUAGE``,
   on every page (console, member, advertiser and Django administration), so it never changes
   during navigation.
4. No choice yet, everyone else: the browser's language (LocaleMiddleware), or the site
   configuration default when the browser expresses none.

The EN/SW switch (``set_language``) sets the cookie and, when signed in, saves the choice on the
account. A saved choice is copied back to the cookie, so the same language continues after signing
out, and the next person to sign in on that device gets their own saved choice, not the previous one.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import translation
from django.views.decorators.http import require_POST
from django.views.i18n import set_language as django_set_language

from .services import get_site_configuration

CONSOLE_PREFIX = "/manage/"


def _supported(code):
    return code if code in dict(settings.LANGUAGES) else ""


def _console_request(request):
    """The console, or Django's sign-in page reached from it (/admin/login/?next=/manage/...)."""
    if request.path.startswith(CONSOLE_PREFIX):
        return True
    admin_login = request.path.startswith("/admin/login/")
    return admin_login and request.GET.get("next", "").startswith(CONSOLE_PREFIX)


def resolve(request):
    """(language, source) for this request; language is "" when LocaleMiddleware's choice stands."""
    user = getattr(request, "user", None)
    signed_in = user is not None and user.is_authenticated
    saved = _supported(getattr(user, "preferred_language", "")) if signed_in else ""
    if saved:
        return saved, "account"
    cookie = _supported(request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME, ""))
    if cookie:
        return cookie, "cookie"
    console = _supported(getattr(settings, "CONSOLE_DEFAULT_LANGUAGE", ""))
    if console and ((signed_in and user.is_staff) or _console_request(request)):
        return console, "console"
    if "HTTP_ACCEPT_LANGUAGE" not in request.META:
        site = _supported(getattr(get_site_configuration(), "default_language", ""))
        if site:
            return site, "site"
    return "", "browser"


def set_language_cookie(response, language):
    response.set_cookie(
        settings.LANGUAGE_COOKIE_NAME, language, max_age=settings.LANGUAGE_COOKIE_AGE,
        path=settings.LANGUAGE_COOKIE_PATH, domain=settings.LANGUAGE_COOKIE_DOMAIN,
        secure=settings.LANGUAGE_COOKIE_SECURE, httponly=settings.LANGUAGE_COOKIE_HTTPONLY,
        samesite=settings.LANGUAGE_COOKIE_SAMESITE,
    )


class LanguagePreferenceMiddleware:
    """Runs after AuthenticationMiddleware (it needs the account) and after LocaleMiddleware, whose
    choice it replaces only when the resolver above says so."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        language, source = resolve(request)
        if language:
            translation.activate(language)
            request.LANGUAGE_CODE = language
        response = self.get_response(request)
        if (source == "account" and request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME) != language
                and settings.LANGUAGE_COOKIE_NAME not in response.cookies):
            set_language_cookie(response, language)
        return response


@require_POST
def set_language(request):
    """Django's CSRF-protected set_language (same form, same safe ``next`` handling) that also saves
    the choice on the signed-in account."""
    response = django_set_language(request)
    language = _supported(request.POST.get("language", ""))
    user = request.user
    if language and user.is_authenticated and user.preferred_language != language:
        get_user_model().objects.filter(pk=user.pk).update(preferred_language=language)
        user.preferred_language = language
    return response


def remember_new_account_language(request, user):
    """A new account keeps the language its owner chose on the sign-up page (if they chose one)."""
    cookie = _supported(request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME, ""))
    if cookie and not user.preferred_language:
        get_user_model().objects.filter(pk=user.pk).update(preferred_language=cookie)
        user.preferred_language = cookie


def for_user(user):
    """The language to write to this person outside a request (emails): their saved choice, else
    the site default language."""
    saved = _supported(getattr(user, "preferred_language", "")) if user is not None else ""
    if saved:
        return saved
    return _supported(getattr(get_site_configuration(), "default_language", "")) or settings.LANGUAGE_CODE
