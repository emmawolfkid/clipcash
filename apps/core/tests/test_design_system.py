"""UI-02 template contracts; no application database access is required."""
from types import SimpleNamespace
import re

from django import forms
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.contrib.staticfiles import finders
from django.core.paginator import Paginator
from django.template import engines
from django.template.loader import get_template, render_to_string
from django.test import RequestFactory, SimpleTestCase
from django.urls import resolve
from django.utils import translation


class DesignSystemTests(SimpleTestCase):
    def context(self, path="/", authenticated=False):
        request = RequestFactory().get(path)
        request.resolver_match = resolve(path)
        user = SimpleNamespace(is_authenticated=True, is_staff=False) if authenticated else AnonymousUser()
        request.user = user
        return {
            "request": request, "user": user,
            "site_config": {"platform_name": "ClipCash"},
            "features": {"memberships": True, "videos": True, "advertisers": True},
            "unread_notifications": 3,
        }

    def test_all_project_html_templates_compile(self):
        for path in (settings.BASE_DIR / "templates").rglob("*.html"):
            with self.subTest(template=str(path)):
                get_template(path.relative_to(settings.BASE_DIR / "templates").as_posix())

    def test_inbox_composes_one_portal_without_editing_its_template(self):
        context = self.context("/notifications/", authenticated=True)
        context.update(page=Paginator([], 20).page(1), categories=[])
        html = render_to_string("notifications/inbox.html", context)
        self.assertEqual(html.count('id="content"'), 1)
        self.assertEqual(html.count('id="member-menu"'), 1)
        self.assertIn('aria-current="page" href="/notifications/"', html)
        self.assertIn('action="/notifications/read-all/"', html)
        self.assertEqual(html.count('src="/static/js/member-nav.js"'), 1)

    def test_membership_shell_for_member_and_public_browsing(self):
        for template, path in (("my_membership", "/memberships/mine/"),
                               ("subscription_detail", "/memberships/mine/00000000-0000-0000-0000-000000000001/"),
                               ("plan_list", "/memberships/"),
                               ("plan_detail", "/memberships/plans/example/")):
            context = self.context(path, authenticated=True)
            context.update(plan={"name": "Example", "code": "example", "price_amount": 1000, "duration_days": 30},
                           subscription={"plan_name": "Example", "price_amount": 1000, "duration_days": 30})
            with self.subTest(template=template):
                html = render_to_string(f"memberships/{template}.html", context)
                self.assertEqual(html.count('id="content"'), 1)
                self.assertIn('id="member-menu"', html)
                self.assertIn('aria-current="page" href="/memberships/mine/"', html)
        html = render_to_string("memberships/plan_list.html", self.context("/memberships/"))
        self.assertNotIn('id="member-menu"', html)
        self.assertIn("Online payment is not available yet", html)

    def test_shared_form_error_links_and_hidden_values(self):
        class SampleForm(forms.Form):
            email = forms.EmailField()
            operation = forms.CharField(widget=forms.HiddenInput)
        form = SampleForm({"email": "invalid", "operation": "retain-this-value"})
        self.assertFalse(form.is_valid())
        html = render_to_string("components/form.html", {"form": form, "button": "Continue"})
        self.assertIn('href="#id_email"', html)
        self.assertIn('aria-invalid="true"', html)
        self.assertIn('value="retain-this-value"', html)
        self.assertIn('class="error-summary" tabindex="-1"', html)

    def test_shell_languages_external_assets_and_financial_notice(self):
        for language in ("en", "sw"):
            with self.subTest(language=language), translation.override(language):
                context = self.context()
                context.update(financial_locked=True, restricted_demo=True)
                html = render_to_string("public/home.html", context)
                self.assertIn(f'<html lang="{language}">', html)
                self.assertIn('lang="sw">Tazama Video · Kusanya Zawadi', html)
                self.assertIn("css/core/tokens.css", html)
                self.assertIn("css/components/forms.css", html)
                self.assertIn('aria-controls="site-menu"', html)
                self.assertIn("No money is paid out on this site", html)
                self.assertNotIn("<style", html)
                self.assertNotIn(" style=", html)

    def test_console_capabilities_still_control_navigation(self):
        template = engines["django"].from_string('{% extends "administration/layout.html" %}')
        context = self.context("/manage/", authenticated=True)
        context["can"] = {"videos": True}
        html = template.render(context)
        self.assertIn('href="/manage/videos/"', html)
        self.assertNotIn('href="/manage/payments/"', html)
        self.assertNotIn('href="/manage/members/"', html)

    def test_stylesheet_order_scope_and_static_resolution(self):
        cases = (
            ("public/home.html", "/", False, "pages/public/home.css", None),
            ("accounts/login.html", "/accounts/login/", False, None, None),
            ("members/activity.html", "/dashboard/activity/", True, None, "member.css"),
            ("advertising/home.html", "/advertisers/", True, None, "advertiser.css"),
            ("administration/overview.html", "/manage/", True, None, "admin.css"),
            ("notifications/inbox.html", "/notifications/", True, "pages/notifications/inbox.css", "member.css"),
            ("memberships/plan_list.html", "/memberships/", True, "pages/memberships/plans.css", "member.css"),
            ("videos/detail.html", "/videos/example/", False, "pages/videos/detail.css", "member.css"),
            ("videos/library.html", "/videos/", False, "pages/videos/library.css", "member.css"),
            ("wallets/wallet.html", "/wallet/", True, "pages/wallets/wallet.css", "member.css"),
            ("administration/videos/detail.html", "/manage/videos/00000000-0000-0000-0000-000000000001/", True, "pages/administration/videos/detail.css", "admin.css"),
            ("viewing/session.html", "/watch/session/00000000-0000-0000-0000-000000000001/", True, "pages/viewing/session.css", "member.css"),
            ("withdrawals/overview.html", "/withdrawals/", True, "pages/withdrawals/overview.css", "member.css"),
        )
        for template, path, authenticated, page_css, layout in cases:
            with self.subTest(template=template):
                context = self.context(path, authenticated)
                context.update(page=Paginator([], 20).page(1), categories=[], can={}, sections={})
                html = render_to_string(template, context)
                styles = re.findall(r'<link rel="stylesheet" href="/static/(css/[^\"]+)"', html)
                self.assertEqual(len(styles), len(set(styles)), styles)
                self.assertEqual(styles[:3], ["css/core/tokens.css", "css/core/base.css", "css/core/typography.css"])
                self.assertEqual([s for s in styles if s.startswith("css/pages/")],
                                 ["css/" + page_css] if page_css else [])
                if page_css:
                    self.assertEqual(styles[-1], "css/" + page_css)
                if layout:
                    self.assertIn("css/layouts/" + layout, styles)
                for style in styles:
                    self.assertIsNotNone(finders.find(style), style)
                self.assertNotIn("css/clipcash.css", html)
                self.assertNotIn("css/design-system.css", html)

    def test_every_css_module_is_referenced_and_has_styles(self):
        template_text = "\n".join(p.read_text(encoding="utf-8") for p in (settings.BASE_DIR / "templates").rglob("*.html"))
        paths = re.findall(r"{% static ['\"](css/[^'\"]+\.css)['\"] %}", template_text)
        for path in paths:
            self.assertIsNotNone(finders.find(path), path)
        for path in (settings.BASE_DIR / "static/css").rglob("*.css"):
            relative = path.relative_to(settings.BASE_DIR / "static").as_posix()
            self.assertIn(relative, paths)
            self.assertIn("{", path.read_text(encoding="utf-8"))
