"""UI-03 presentation contracts, without application database access."""
from html.parser import HTMLParser
from urllib.parse import urlsplit

from django import forms
from django.conf import settings
from django.template.loader import render_to_string
from django.test import SimpleTestCase, RequestFactory, override_settings
from django.urls import resolve
from django.utils import translation
from django.views.i18n import set_language

from .test_design_system import DesignSystemTests


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.items = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.items.append((tag, dict(attrs)))


class PublicAuthUITests(SimpleTestCase):
    def context(self, path="/", authenticated=False):
        context = DesignSystemTests().context(path, authenticated)
        context.update(financial_locked=True, csrf_token="test-token", next="/videos/")
        return context

    def test_landing_links_resolve_and_fragment_targets_exist(self):
        html = render_to_string("public/home.html", self.context())
        elements = Elements(html).items
        ids = [attrs["id"] for _, attrs in elements if "id" in attrs]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sum(tag == "h1" for tag, _ in elements), 1)
        self.assertEqual(sum(tag == "details" for tag, _ in elements), 5)
        for tag, attrs in elements:
            if tag == "a":
                target = urlsplit(attrs.get("href", ""))
                if target.path.startswith("/"):
                    resolve(target.path)
                if target.fragment and target.path in ("", "/"):
                    self.assertIn(target.fragment, ids)
        for section in ("how-it-works", "benefits-title", "advertiser-title", "questions"):
            self.assertIn(section, ids)
        self.assertIn("sandbox payments do not activate access", html)
        self.assertNotIn("/privacy/", html)
        self.assertNotIn("/terms/", html)

    def test_feature_flags_control_public_navigation_and_actions(self):
        context = self.context()
        context["features"] = dict.fromkeys(("registration", "videos", "memberships", "advertisers"), False)
        html = render_to_string("public/home.html", context)
        for route in ("/accounts/register/", "/videos/", "/memberships/", "/advertisers/"):
            self.assertNotIn(f'href="{route}"', html)
        self.assertIn('href="/accounts/login/"', html)
        self.assertIn("No money is paid out on this site", html)

    def test_every_auth_state_uses_one_main_and_existing_forms(self):
        class SampleForm(forms.Form):
            email = forms.EmailField()
            password = forms.CharField(widget=forms.PasswordInput)
            operation = forms.CharField(widget=forms.HiddenInput, initial="preserve-operation")
        for template in (settings.BASE_DIR / "templates/accounts").glob("*.html"):
            if template.stem in ("layout", "profile", "profile_edit", "email_change_form"):  # Member layout.
                continue
            with self.subTest(template=template.name):
                context = self.context("/accounts/login/")
                context.update(form=SampleForm(), validlink=True)
                html = render_to_string("accounts/" + template.name, context)
                self.assertEqual(html.count('id="content"'), 1)
                self.assertEqual(sum(tag == "h1" for tag, _ in Elements(html).items), 1)
                self.assertIn("css/layouts/auth.css", html)
                self.assertIn('src="/static/js/auth.js"', html)
                self.assertNotIn(" style=", html)
                self.assertNotIn("<style", html)
                if 'class="form"' in html:
                    self.assertIn('name="email"', html)
                    self.assertIn('name="password"', html)
                    self.assertIn('value="preserve-operation"', html)
                    self.assertIn('name="csrfmiddlewaretoken"', html)
                    self.assertTrue(any(tag == "input" and attrs.get("name") == "next"
                                        and attrs.get("type") == "hidden" and attrs.get("value") == "/videos/"
                                        for tag, attrs in Elements(html).items))
                    self.assertIn('data-password-target="id_password"', html)
                    self.assertIn('hidden aria-pressed="false" aria-controls="id_password"', html)

    def test_invalid_password_link_does_not_render_a_password_form(self):
        for template in ("activate_account", "password_reset_confirm"):
            html = render_to_string(f"accounts/{template}.html", self.context())
            self.assertNotIn('name="password"', html)
            self.assertIn("invalid, has expired or has already been used", html)

    def test_auth_styles_and_script_do_not_load_in_member_shell(self):
        html = render_to_string("members/activity.html", self.context("/dashboard/activity/", True))
        self.assertNotIn("css/layouts/auth.css", html)
        self.assertNotIn("js/auth.js", html)

    def test_languages_keep_valid_navigation_and_external_assets(self):
        for language in ("en", "sw"):
            with self.subTest(language=language), translation.override(language):
                html = render_to_string("public/home.html", self.context())
                self.assertIn(f'<html lang="{language}">', html)
                self.assertIn(f'value="{language}" selected', html)
                self.assertIn('action="/i18n/setlang/"', html)
                self.assertIn('lang="sw">Tazama Video · Kusanya Zawadi', html)

    @override_settings(ALLOWED_HOSTS=["testserver"])
    def test_language_switch_sets_cookie_and_preserves_safe_destination(self):
        for language in ("en", "sw"):
            request = RequestFactory().post("/i18n/setlang/", {"language": language, "next": "/accounts/login/"})
            response = set_language(request)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, "/accounts/login/")
            self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, language)
