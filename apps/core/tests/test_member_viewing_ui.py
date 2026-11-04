"""UI-04 presentation contracts, alongside existing database access/evidence tests."""
from types import SimpleNamespace
from uuid import UUID

from django.core.paginator import Paginator
from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import translation

from .test_design_system import DesignSystemTests


class MemberViewingUITests(SimpleTestCase):
    def context(self, path="/dashboard/"):
        context = DesignSystemTests.context(self, path, authenticated=True)
        context["user"].email = "member@example.test"
        context["user"].first_name = "Member"
        return context

    def test_dashboard_shortcuts_follow_feature_and_summary_flags(self):
        context = self.context()
        context.update(summary={"sponsored": {"enabled": False}, "withdrawals": {"destinations": 0}})
        html = render_to_string("members/dashboard.html", context)
        self.assertIn("Explore videos", html)
        self.assertIn(reverse("notifications:inbox"), html)
        self.assertIn("Real rewards are not available yet", html)
        welcome = html.split('<header class="dashboard-welcome">', 1)[1].split("</header>", 1)[0]
        self.assertNotIn('href="/watch/"', welcome)
        self.assertNotIn("TZS 0", html)
        context["features"] = {}
        html = render_to_string("members/dashboard.html", context)
        self.assertNotIn("Explore videos", html)

    def test_library_category_search_empty_and_pagination(self):
        context = self.context("/videos/category/news/")
        context.update(category={"pk": 1, "slug": "news", "name": "News"},
                       categories=[{"pk": 1, "slug": "news", "name": "News"}], query="no match")
        html = render_to_string("videos/library.html", context)
        self.assertIn('aria-current="page" href="/videos/category/news/"', html)
        self.assertIn('value="no match"', html)
        self.assertIn("No videos match your search", html)
        self.assertIn('href="/videos/category/news/">Clear search', html)
        context.update(videos=[{"slug": "example", "title": "Example"}],
                       page=Paginator([1, 2], 1).page(1), query="a&b")
        html = render_to_string("videos/library.html", context)
        self.assertIn("q=a%26b&amp;page=2", html)
        self.assertIn('class="video-card__placeholder"', html)

    def test_playback_denial_does_not_render_media(self):
        context = self.context("/videos/example/")
        context.update(video={"title": "Example"}, playback_reason="no_membership",
                       playback_message="An active membership is needed to watch this video.")
        html = render_to_string("videos/detail.html", context)
        self.assertIn("See membership plans", html)
        self.assertNotIn("<iframe", html)
        self.assertNotIn("<video", html)
        self.assertIn("does not verify a sponsored view", html)

    def session(self, outcome="", is_open=True):
        return SimpleNamespace(pk=UUID(int=1), campaign=SimpleNamespace(name="Campaign"),
                               video=SimpleNamespace(title="Video"), is_open=is_open,
                               outcome=outcome, progress_percent=25, credited_ms=3000,
                               required_ms=12000, get_outcome_display=outcome)

    def test_open_session_preserves_post_contract_and_server_progress(self):
        context = self.context("/watch/session/00000000-0000-0000-0000-000000000001/")
        context.update(session=self.session(), playback={"kind": "video", "url": "/sample.mp4"},
                       heartbeat_seconds=10)
        for language in ("en", "sw"):
            with self.subTest(language=language), translation.override(language):
                html = render_to_string("viewing/session.html", context)
                self.assertIn('data-events-url="/watch/session/00000000-0000-0000-0000-000000000001/events/"', html)
                self.assertIn('data-heartbeat="10"', html)
                self.assertIn('max="100" value="25"', html)
                self.assertIn('id="watch-video"', html)
                self.assertIn('data-unconfirmed=', html)
                self.assertIn("does not confirm a verified view or a reward", html)
                self.assertNotIn(" style=", html)

    def test_closed_outcomes_and_unavailable_playback(self):
        context = self.context()
        for outcome, text in (("VERIFIED_EVIDENCE", "This view was verified"),
                              ("SUSPICIOUS_REVIEW", "being reviewed"),
                              ("REJECTED", "could not be accepted"),
                              ("EXPIRED", "session expired"),
                              ("INSUFFICIENT_EVIDENCE", "Not enough")):
            with self.subTest(outcome=outcome):
                context.update(session=self.session(outcome, False))
                html = render_to_string("viewing/session.html", context)
                self.assertIn(text, html)
                self.assertNotIn('id="watch-panel"', html)
                self.assertNotIn("js/video-watch.js", html)
        context.update(session=self.session(), message="Playback unavailable")
        html = render_to_string("viewing/session.html", context)
        self.assertIn("Playback unavailable", html)
        self.assertNotIn('id="watch-frame"', html)

    def test_history_keeps_column_meanings_and_empty_recovery(self):
        context = self.context("/watch/history/")
        context["page"] = Paginator([self.session("EXPIRED", False)], 20).page(1)
        html = render_to_string("viewing/history.html", context)
        self.assertIn("Your sponsored viewing sessions</caption>", html)
        self.assertEqual(html.count('scope="col"'), 4)
        self.assertIn("3 / 12 s", html)
        context["page"] = Paginator([], 20).page(1)
        html = render_to_string("viewing/history.html", context)
        self.assertIn("You have not watched any sponsored videos yet", html)
        self.assertIn("Find sponsored videos", html)
