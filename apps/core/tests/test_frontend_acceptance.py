"""UI-08: frontend acceptance corrections that browsers found and tests should keep."""

from pathlib import Path
from tempfile import TemporaryDirectory

from django.conf import settings
from django.template import Context, Template
from django.test import Client, SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import translation

from apps.accounts.tests.helpers import make_user
from apps.support import services as support
from apps.support.forms import new_submission_key
from apps.withdrawals.tests.helpers import WithdrawalsTestCase

TEMPLATES = settings.BASE_DIR / "templates"
STATIC = settings.BASE_DIR / "static"


def offered():
    template = Template("{% load language_tags %}{% switchable_languages as langs %}"
                        "{% for code, name in langs %}{{ code }} {% endfor %}")
    return template.render(Context()).split()


class LanguageSwitcherTests(SimpleTestCase):
    def test_a_language_without_a_catalogue_is_not_offered(self):
        with TemporaryDirectory() as empty, override_settings(LOCALE_PATHS=[empty]), translation.override("en"):
            self.assertEqual(offered(), ["en"])

    def test_a_visitor_already_using_it_can_switch_back(self):
        with TemporaryDirectory() as empty, override_settings(LOCALE_PATHS=[empty]), translation.override("sw"):
            self.assertEqual(offered(), ["en", "sw"])

    def test_adding_a_compiled_catalogue_brings_it_back(self):
        with TemporaryDirectory() as root, override_settings(LOCALE_PATHS=[root]), translation.override("en"):
            catalogue = Path(root) / "sw" / "LC_MESSAGES"
            catalogue.mkdir(parents=True)
            (catalogue / "django.mo").write_bytes(b"")
            self.assertEqual(offered(), ["en", "sw"])


class SharedFrontendTests(SimpleTestCase):
    def test_favicon_is_linked_and_uses_the_brand_colours(self):
        self.assertIn("images/favicon.svg", (TEMPLATES / "base.html").read_text(encoding="utf-8"))
        icon = (STATIC / "images/favicon.svg").read_text(encoding="utf-8")
        self.assertIn("#FFCF19", icon)
        self.assertIn("#111B46", icon)

    def test_scrolling_tables_get_a_translatable_visual_hint(self):
        self.assertIn('data-table-scroll-hint="{% translate', (TEMPLATES / "base.html").read_text(encoding="utf-8"))
        script = (STATIC / "js/shared-ui.js").read_text(encoding="utf-8")
        self.assertIn('hint.setAttribute("aria-hidden", "true")', script)
        self.assertIn("hint.hidden = !scrolls", script)

    def test_keyboard_focus_brings_a_clipped_table_action_into_view(self):
        script = (STATIC / "js/shared-ui.js").read_text(encoding="utf-8")
        self.assertIn('region.addEventListener("focusin"', script)
        self.assertIn('event.target.matches(":focus-visible")', script)

    def test_escape_moves_focus_out_of_the_menu_before_closing_it(self):
        script = (STATIC / "js/member-nav.js").read_text(encoding="utf-8")
        handler = script.split('event.key === "Escape"', 1)[1]
        self.assertLess(handler.index("toggle.focus()"), handler.index('toggle.setAttribute("aria-expanded", "false")'))

    def test_invalid_text_inputs_outrank_the_general_input_border(self):
        forms = (STATIC / "css/components/forms.css").read_text(encoding="utf-8")
        self.assertIn('.form input[aria-invalid="true"]:not([type="hidden"]):not([type="checkbox"]):not([type="radio"])', forms)

    def test_auth_pages_have_no_heading_before_their_title(self):
        layout = (TEMPLATES / "accounts/layout.html").read_text(encoding="utf-8")
        self.assertNotIn("<h2", layout.split("auth_content", 1)[0])
        self.assertIn('id="auth-intro-title"', layout)

    def test_library_cards_sit_under_a_heading_level_that_follows_the_title(self):
        library = (TEMPLATES / "videos/library.html").read_text(encoding="utf-8")
        self.assertLess(library.index('<h2 class="visually-hidden">'), library.index('class="video-grid"'))


class MemberPageTests(WithdrawalsTestCase):
    def test_support_list_has_a_caption(self):
        support.create_ticket(self.member, subject="A sample subject", category="VIDEOS",
                              description="The player stays black on my phone.", submission_key=new_submission_key())
        self.client.force_login(self.member)
        self.assertContains(self.client.get(reverse("support:tickets")), "Your support tickets")

    def test_removing_a_destination_asks_first_and_still_posts_the_same_fields(self):
        beneficiary = self.mobile(number="0687 123 456")
        self.client.force_login(self.member)
        page = self.client.get(reverse("withdrawals:destinations"))
        self.assertContains(page, "Confirm removal")
        self.assertContains(page, "This destination will no longer be available")
        self.assertContains(page, 'name="action" value="remove"')
        self.assertContains(page, "csrfmiddlewaretoken")
        self.assertNotContains(page, "687123456")
        self.client.post(reverse("withdrawals:destinations"), {"action": "remove", "beneficiary": beneficiary.pk})
        beneficiary.refresh_from_db()
        self.assertEqual(beneficiary.status, "DEACTIVATED")

    def test_real_error_pages_render_with_the_site_layout(self):
        response = Client().get("/no-such-page/")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Page not found", status_code=404)
        self.assertContains(response, 'class="site-header"', status_code=404)

    def test_restricted_member_sees_why_a_video_cannot_play(self):
        self.client.force_login(make_user("no-plan@example.com"))
        response = self.client.get(reverse("videos:detail", args=[self.ad_video.slug]))
        self.assertContains(response, "An active membership is needed to watch this video.")
        self.assertContains(response, reverse("memberships:plans"))
