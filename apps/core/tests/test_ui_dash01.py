"""UI-DASH-01 member portal shell and dashboard contracts.

Template tests need no database; the "verified today" count is checked against real
sponsored-viewing records. Browser behaviour (collapse, drawer, carousel) was verified
separately in headless Chrome; member-nav.js keeps its Node DOM-mock test for console shells.
"""
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.utils import timezone, translation

from .test_dashboard_profile_ui import DashboardProfileUITests
from .test_design_system import DesignSystemTests
from .test_public_auth_ui import Elements

PLAN_ID = UUID("00000000-0000-0000-0000-0000000000a1")


class PortalShellTests(SimpleTestCase):
    def context(self, path, authenticated=True, **extra):
        context = DesignSystemTests().context(path, authenticated=authenticated)
        context.update({"csrf_token": "test", "nav": "dashboard", **extra})
        return context

    def test_member_pages_use_the_collapsible_portal_shell(self):
        html = render_to_string("members/activity.html", self.context("/dashboard/activity/", nav="activity", activity=[]))
        self.assertIn('<body class="portal"', html)
        self.assertIn('class="site-header portal-header"', html)
        self.assertIn('app-shell app-shell--portal', html)
        self.assertIn("data-collapsible", html)
        self.assertIn('data-drawer-query="(max-width: 63.99rem)"', html)
        self.assertEqual(html.count('src="/static/js/sidebar-state.js"'), 1)
        self.assertIn('/static/css/layouts/member-portal.css', html)
        self.assertEqual(html.count('class="site-menu-toggle'), 1)
        self.assertIn('aria-controls="member-menu"', html)
        for label in ("Collapse sidebar", "Expand sidebar", "Open navigation"):
            self.assertIn(label, html)
        self.assertNotIn(" style=", html)

    def test_protected_inbox_gets_the_same_portal_shell(self):
        from django.core.paginator import Paginator
        context = self.context("/notifications/", nav="notifications")
        context.update(page=Paginator([], 20).page(1), categories=[])
        html = render_to_string("notifications/inbox.html", context)
        self.assertIn('<body class="portal"', html)
        self.assertIn("portal-header", html)
        self.assertIn("app-shell--portal", html)
        self.assertEqual(html.count('id="member-menu"'), 1)

    def test_console_and_public_pages_keep_their_existing_chrome(self):
        # OPS-01: the console now uses the approved portal shell (its own menu and brand link).
        console = render_to_string("administration/layout.html", self.context("/manage/", can={}))
        self.assertIn('class="portal portal--console"', console)
        self.assertEqual(console.count('id="console-menu"'), 1)
        self.assertIn('aria-controls="console-menu"', console)
        public = render_to_string("memberships/plan_list.html",
                                  self.context("/memberships/", authenticated=False))
        for html in (public,):
            self.assertNotIn('class="portal"', html)
            self.assertNotIn("portal-header", html)
            self.assertNotIn("sidebar-state.js", html)
            self.assertNotIn("data-collapsible", html)
            self.assertIn('<header class="site-header">', html)

    def test_sidebar_groups_order_and_unread_count(self):
        html = render_to_string("components/member_sidebar.html", self.context("/dashboard/", sandbox_payments=False,
                                                                               unread_notifications=105))
        order = ["/dashboard/", "/videos/", "/watch/", "/dashboard/activity/", "/rewards/", "/memberships/mine/",
                 "/wallet/", "/withdrawals/", "/referrals/", "/advertisers/", "/notifications/", "/support/",
                 "/accounts/"]
        positions = [html.index(f'href="{route}"') for route in order]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(html.count('class="app-nav__list"'), 3)
        self.assertIn(">99+<", html)
        self.assertIn("Settings", html)
        self.assertIn('href="/referrals/"', html)  # REF-01: the referral programme exists now.
        # Every visible label is a real element (it doubles as the collapsed tooltip).
        self.assertEqual(html.count('class="app-nav__label"'), 14)


class DashboardRedesignTests(SimpleTestCase):
    def context(self):
        context = DashboardProfileUITests().context()
        context["dashboard_plans"] = [
            {"pk": UUID(int=1), "name": "Starter", "code": "starter", "price_amount": 1500, "duration_days": 7, "benefits": []},
            {"pk": PLAN_ID, "name": "Silver", "code": "silver", "price_amount": 10000, "duration_days": 30,
             "benefits": ["HD playback"]},
        ]
        context["summary"]["membership"]["subscription"]["plan_id"] = PLAN_ID
        context["summary"]["sponsored"]["today"] = 2
        context["features"] = {**context["features"], "real_money_rewards": False, "withdrawals": False}
        return context

    def render(self, context=None, language="en"):
        with translation.override(language):
            return render_to_string("members/dashboard.html", context or self.context())

    def test_reference_sections_render_in_order(self):
        html = self.render()
        sections = ['class="dashboard-welcome"', 'class="dashboard-summary"', 'class="dashboard-quick"',
                    'class="dashboard-columns"', 'class="panel dashboard-videos"', 'class="panel dashboard-activity"',
                    'class="panel dashboard-plans"', 'class="dashboard-lower"', 'class="dashboard-how"']
        positions = [html.index(marker) for marker in sections if marker in html]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("/static/js/member-dashboard.js", html)
        self.assertIn("/static/images/dashboard/welcome-phone.svg", html)
        self.assertNotIn(" style=", html)

    def test_cards_show_real_values_and_honest_unavailable_states(self):
        html = self.render()
        self.assertIn("Welcome back, Member", html)
        self.assertIn("TZS 711", html)
        self.assertIn('data-balance-toggle aria-pressed="false" hidden', html)
        self.assertIn("Verified today", html)
        self.assertIn(">2<", html)
        self.assertIn("Withdrawal information", html)
        self.assertNotIn("Withdraw now", html)
        self.assertIn("Review verified activity", html)
        self.assertIn("Check withdrawal availability", html)
        self.assertNotIn("Collect your rewards", html)
        for sample in ("24,500", "1,440", "Silas", "CC204589", "TSh 30", "Pata Pesa"):
            self.assertNotIn(sample, html)

    def test_current_plan_is_marked_and_others_link_to_details(self):
        html = self.render()
        silver = html.split('plan-tier--2 is-current', 1)[1].split('class="plan-tier ', 1)[0]
        self.assertIn('<span class="plan-tier__state">Current</span>', silver)
        self.assertNotIn("/memberships/plans/silver/", html)
        self.assertIn('href="/memberships/plans/starter/"', html)
        self.assertNotIn("/choose/", html)

    def test_referral_has_no_link_and_a_disabled_share_button(self):
        html = self.render()
        referral = html.split('class="panel dashboard-referral"', 1)[1].split("</section>", 1)[0]
        self.assertIn("disabled", referral)
        self.assertNotIn("href=", referral)
        self.assertNotIn("clipcash.co.tz/ref", html)

    def test_withdrawal_methods_come_from_supported_destinations(self):
        context = self.context()
        context["dashboard_networks"] = [("MPESA", "Vodacom M-Pesa"), ("AIRTEL", "Airtel Money")]
        html = self.render(context)
        self.assertIn("payout-method--mpesa", html)
        self.assertIn("payout-method--airtel", html)
        self.assertNotIn("payout-method--mixx", html)

    def test_enabled_withdrawals_switch_the_actions(self):
        context = self.context()
        context["summary"]["withdrawals"]["available"] = True
        context["features"]["withdrawals"] = True
        html = self.render(context)
        self.assertIn("Withdraw now", html)
        self.assertNotIn("Check withdrawal availability", html)

    def test_carousel_controls_only_for_several_videos(self):
        context = self.context()
        video = {"slug": "one", "title": "One", "duration_display": "1:35"}
        context.update(recent_videos=[video])
        self.assertNotIn("data-carousel-dots", self.render(context))
        context.update(recent_videos=[video, {**video, "slug": "two", "title": "Two"}])
        html = self.render(context)
        self.assertEqual(html.count("data-carousel-dot="), 2)
        self.assertIn('href="/videos/two/"', html)
        self.assertIn("1:35", html)

    def test_swahili_dashboard(self):
        html = self.render(language="sw")
        for text in ("Karibu tena, Member", "Salio lako", "Video zilizotazamwa", "Zilizothibitishwa leo",
                     "Vifurushi vya uanachama", "Chagua", "Mipangilio", "Kunja menyu ya pembeni"):
            self.assertIn(text, html)
        self.assertIn("TZS 711", html)


from apps.viewing.tests.helpers import ViewingTestCase  # noqa: E402  (database-backed helpers)


class ActivityTranslationTests(ViewingTestCase):
    """Recent-activity sentences follow the member's language; recorded names stay as data."""

    def verified_view(self):
        session = self.start()
        self.event(session, 1, 0)
        for seq, position in enumerate(range(10, 100, 10), start=2):
            self.event(session, seq, min(position, 95), after=10)
        self.event(session, 12, 95, state="ended", after=1)

    def texts(self, language):
        from apps.core.dashboard import member_activity
        with translation.override(language):
            return [item.text for item in member_activity(self.member)]

    def test_activity_labels_in_english_and_swahili(self):
        from apps.wallets import wallet_services
        self.verified_view()
        wallet_services.ensure_wallet(self.member)
        english, swahili = self.texts("en"), self.texts("sw")
        name = self.campaign.name
        self.assertIn(f"{name}: View verified", english)
        self.assertIn("Wallet opened", english)
        self.assertTrue(any(text.startswith("Membership Monthly: ") for text in english))
        self.assertIn(f"{name}: Utazamaji umethibitishwa", swahili)
        self.assertIn("Pochi imefunguliwa", swahili)
        self.assertTrue(any(text.startswith("Uanachama Monthly: ") for text in swahili))
        self.assertTrue(any(text.startswith("Akaunti imeundwa") for text in swahili))
        self.assertFalse([text for text in swahili if text in english and text != name])


class VerifiedTodayCountTests(SimpleTestCase):
    """The dashboard read model counts today's verified sponsored views separately from the total."""

    def test_today_count_uses_local_midnight_and_verified_outcome(self):
        from unittest import mock

        from apps.core import dashboard
        from apps.viewing.models import WatchSession

        captured = {}

        class Query:
            def aggregate(self, **kwargs):
                captured.update(kwargs)
                return {"today": 1, "verified": 4, "review": 0, "total": 5}

        with mock.patch("apps.core.dashboard.features.is_enabled", return_value=True), \
             mock.patch("apps.viewing.services.available_campaigns", return_value=[]), \
             mock.patch.object(WatchSession.objects, "filter", return_value=Query()):
            result = dashboard._sponsored(SimpleNamespace(pk=1))
        self.assertEqual((result["today"], result["verified"]), (1, 4))
        today = captured["today"].filter
        conditions = dict(today.children)
        self.assertEqual(conditions["outcome"], WatchSession.Outcome.VERIFIED_EVIDENCE)
        start = conditions["finalized_at__gte"]
        local = timezone.localtime()
        self.assertEqual(start, local.replace(hour=0, minute=0, second=0, microsecond=0))
        self.assertLessEqual(local - start, timedelta(days=1))
