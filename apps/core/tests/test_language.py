"""ACC-01: one language preference for the whole site (apps/core/language.py).

An explicit choice (the EN/SW switch) never changes during navigation, redirects, form posts or between
workspaces; it is saved on the signed-in account and restored at the next sign-in, without one person
inheriting another's saved choice on a shared device. Staff with no choice see the console default
everywhere, not only on /manage/.
"""

from django.conf import settings
from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import translation

from apps.advertising.tests.helpers import AdvertisingTestCase, User, make_user

COOKIE = settings.LANGUAGE_COOKIE_NAME
CONSOLE = reverse("administration:overview")


def lang(response):
    return response.headers.get("Content-Language")


@override_settings(CONSOLE_DEFAULT_LANGUAGE="sw")
class LanguagePreferenceTests(AdvertisingTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(translation.activate, translation.get_language())

    def choose(self, code, next_url="/"):
        response = self.client.post(reverse("set_language"), {"language": code, "next": next_url})
        self.assertEqual(response.status_code, 302)
        return response

    def test_explicit_english_stays_english_in_every_workspace(self):
        self.client.force_login(self.root)
        self.choose("en", CONSOLE)
        for url in (CONSOLE, reverse("administration:members"), reverse("dashboard"), reverse("wallets:wallet"),
                    reverse("admin:index"), reverse("notifications:inbox"), reverse("accounts:profile")):
            with self.subTest(url=url):
                self.assertEqual(lang(self.client.get(url, HTTP_ACCEPT_LANGUAGE="sw")), "en")

    def test_explicit_swahili_stays_swahili_including_redirects_and_posts(self):
        self.client.force_login(self.member)
        self.choose("sw", reverse("dashboard"))
        response = self.client.get(reverse("dashboard"), HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(lang(response), "sw")
        # A redirect and the page it leads to keep the choice.
        response = self.client.get(CONSOLE, HTTP_ACCEPT_LANGUAGE="en", follow=True)
        self.assertEqual(lang(response), "sw")
        # An invalid form post re-renders in the chosen language.
        response = self.client.post(reverse("accounts:profile_edit"), {"first_name": ""}, HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(lang(response), "sw")

    def test_staff_without_a_choice_see_the_console_default_on_every_page(self):
        self.client.force_login(self.root)
        for url in (CONSOLE, reverse("dashboard"), reverse("admin:index"), reverse("accounts:profile")):
            with self.subTest(url=url):
                self.assertEqual(lang(self.client.get(url, HTTP_ACCEPT_LANGUAGE="en")), "sw")

    def test_console_sign_in_page_uses_the_console_default_other_sign_in_pages_the_browser(self):
        self.assertEqual(lang(self.client.get(reverse("admin:login") + "?next=/manage/", HTTP_ACCEPT_LANGUAGE="en")), "sw")
        self.assertEqual(lang(self.client.get(reverse("accounts:login"), HTTP_ACCEPT_LANGUAGE="en")), "en")
        self.assertEqual(lang(self.client.get(reverse("accounts:register"), HTTP_ACCEPT_LANGUAGE="sw")), "sw")

    def test_members_without_a_choice_follow_the_browser(self):
        self.client.force_login(self.member)
        self.assertEqual(lang(self.client.get(reverse("dashboard"), HTTP_ACCEPT_LANGUAGE="en")), "en")
        self.assertEqual(lang(self.client.get(reverse("dashboard"), HTTP_ACCEPT_LANGUAGE="sw")), "sw")

    def test_choice_is_saved_on_the_account_and_restored_after_signing_in_again(self):
        self.client.force_login(self.member)
        self.choose("sw")
        self.member.refresh_from_db()
        self.assertEqual(self.member.preferred_language, "sw")
        self.client.post(reverse("accounts:logout"))
        # The device keeps the language after signing out.
        self.assertEqual(lang(self.client.get(reverse("accounts:login"), HTTP_ACCEPT_LANGUAGE="en")), "sw")
        # A different browser (no cookie): the saved choice comes back at sign-in and is remembered.
        from django.test import Client

        other_browser = Client()
        other_browser.force_login(self.member)
        response = other_browser.get(reverse("dashboard"), HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(lang(response), "sw")
        self.assertEqual(response.cookies[COOKIE].value, "sw")

    def test_people_sharing_a_device_keep_their_own_saved_choice(self):
        english = make_user("english.member@example.com")
        self.client.force_login(self.member)
        self.choose("sw")
        self.client.post(reverse("accounts:logout"))
        self.client.force_login(english)
        self.choose("en")
        self.client.post(reverse("accounts:logout"))
        self.client.force_login(self.member)
        self.assertEqual(lang(self.client.get(reverse("dashboard"), HTTP_ACCEPT_LANGUAGE="en")), "sw")
        english.refresh_from_db()
        self.member.refresh_from_db()
        self.assertEqual((self.member.preferred_language, english.preferred_language), ("sw", "en"))

    def test_anonymous_choice_changes_no_account(self):
        self.choose("sw", reverse("accounts:login"))
        self.assertEqual(lang(self.client.get(reverse("accounts:login"), HTTP_ACCEPT_LANGUAGE="en")), "sw")
        self.assertFalse(User.objects.exclude(preferred_language="").exists())

    def test_unsupported_language_is_ignored(self):
        self.client.force_login(self.member)
        self.client.post(reverse("set_language"), {"language": "fr", "next": "/"})
        self.member.refresh_from_db()
        self.assertEqual(self.member.preferred_language, "")
        self.client.cookies[COOKIE] = "fr"
        self.assertEqual(lang(self.client.get(reverse("dashboard"), HTTP_ACCEPT_LANGUAGE="en")), "en")

    def test_set_language_still_requires_post(self):
        self.assertEqual(self.client.get(reverse("set_language")).status_code, 405)


class EmailLanguageTests(AdvertisingTestCase):
    def test_emails_are_built_in_the_recipients_saved_language(self):
        from types import SimpleNamespace

        from apps.notifications import services

        seen = []
        kind = services.EMAIL_KINDS[services.TEST_EMAIL]

        def builder(email):
            seen.append(translation.get_language())
            return kind.builder(email)

        self.member.preferred_language = "sw"
        self.member.save(update_fields=["preferred_language"])
        services.EMAIL_KINDS[services.TEST_EMAIL] = kind._replace(builder=builder) if hasattr(kind, "_replace")             else type(kind)(kind.name, builder, kind.ttl_seconds, kind.context_keys)
        self.addCleanup(services.EMAIL_KINDS.__setitem__, services.TEST_EMAIL, kind)
        for user in (self.member, None):
            email = services.enqueue_email(services.TEST_EMAIL, recipient="member@example.com", user=user)
            services.deliver_email({"email_id": str(email.pk)}, SimpleNamespace(attempts=1))
        self.assertEqual(seen, ["sw", settings.LANGUAGE_CODE])
        self.assertEqual(len(mail.outbox), 2)
