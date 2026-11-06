"""Phase 15-D: staff analytics (access, correctness, filters, empty states, money separation)."""

import re
from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation

from apps.accounts.tests.helpers import AccountsTestCase, User, make_user
from apps.administration import summary as admin_summary
from apps.audit.models import AuditEvent
from apps.core.models import SiteConfiguration
from apps.payments.tests.helpers import PaymentsTestCase
from apps.reports import analytics, services
from apps.rewards import services as rewards
from apps.rewards.tests.helpers import RewardsTestCase
from apps.support import services as support
from apps.support.forms import new_submission_key
from apps.viewing.services import campaign_performance
from apps.withdrawals.tests.helpers import WithdrawalsTestCase

URL = reverse("administration:analytics")
OPERATIONAL = {"members": {"memberships"}, "advertising": {"campaigns", "viewing"}, "support": {"support"},
               "operations": {"operations"}}
FINANCIAL = {"advertising": {"budget", "real"}, "rewards": {"simulated"}, "payments": {"sandbox", "real"},
             "withdrawals": {"demo"}}


def grant(user, *codenames):
    user.user_permissions.add(*Permission.objects.filter(content_type__app_label="reports", codename__in=codenames))
    return User.objects.get(pk=user.pk)


def staff_with(email, *codenames):
    return grant(make_user(email, role=User.Role.CONTENT_MANAGER), *codenames)


def by_key(sections):
    return {section.key: section for section in sections}


def cards(section):
    return {str(card.label): card.value for card in section.cards}


class AccessTests(AccountsTestCase):
    def test_outsiders_are_kept_out(self):
        self.assertEqual(self.client.get(URL).status_code, 302)
        self.client.force_login(make_user("member@example.com"))
        self.assertEqual(self.client.get(URL).status_code, 302)  # To the admin login.

    def test_staff_without_a_report_permission_are_refused(self):
        self.client.force_login(make_user("staff@example.com", role=User.Role.CONTENT_MANAGER))
        self.assertEqual(self.client.get(URL).status_code, 403)
        self.client.force_login(staff_with("audit@example.com", "view_audit_report"))  # Audit has no analytics.
        self.assertEqual(self.client.get(URL).status_code, 403)

    def test_each_permission_shows_only_its_own_sections(self):
        for slug in ("members", "advertising", "rewards", "payments", "withdrawals", "operations", "support"):
            self.client.force_login(staff_with(f"{slug}@example.com", f"view_{slug}_report"))
            response = self.client.get(URL)
            with self.subTest(report=slug):
                self.assertEqual(response.status_code, 200)
                self.assertEqual(set(by_key(response.context["sections"])), OPERATIONAL.get(slug, set()))
                self.assertEqual(set(by_key(response.context["financial"])), FINANCIAL.get(slug, set()))

    def test_navigation_entry_only_for_authorized_staff(self):
        link = f'href="{URL}"'
        self.client.force_login(staff_with("audit@example.com", "view_audit_report"))
        self.assertNotContains(self.client.get(reverse("administration:reports")), link)
        self.client.force_login(staff_with("support@example.com", "view_support_report"))
        self.assertContains(self.client.get(reverse("administration:reports")), link)
        current = self.client.get(URL)
        self.assertContains(current, f'aria-current="page" {link}')

    def test_super_administrator_sees_everything(self):
        self.client.force_login(make_user("root@example.com", role=User.Role.SUPER_ADMIN))
        response = self.client.get(URL)
        self.assertEqual(set(by_key(response.context["sections"])),
                         {"memberships", "campaigns", "viewing", "support", "operations"})
        self.assertEqual(set(by_key(response.context["financial"])), {"simulated", "sandbox", "demo", "budget", "real"})


class FilterAndTrendTests(AccountsTestCase):
    def test_grain_follows_the_period_length(self):
        for first, last, grain, count in [("2026-03-01", "2026-03-31", analytics.DAY, 31),
                                          ("2026-01-01", "2026-03-31", analytics.WEEK, 14),
                                          ("2025-12-15", "2026-11-14", analytics.MONTH, 12)]:
            period = services.period_from(first, last)
            with self.subTest(first=first):
                self.assertEqual(analytics.grain_for(period), grain)
                self.assertEqual(len(analytics.buckets_for(period, grain)), count)

    def test_buckets_are_zero_filled_and_follow_the_site_time_zone(self):
        SiteConfiguration.objects.filter(id=1).update(default_timezone="Africa/Dar_es_Salaam")
        cache.clear()
        member = make_user("night@example.com")
        # 21:30 UTC on 1 March is 00:30 on 2 March in Dar es Salaam.
        User.objects.filter(pk=member.pk).update(date_joined=datetime(2026, 3, 1, 21, 30, tzinfo=dt_timezone.utc))
        period = services.period_from("2026-03-01", "2026-03-03")
        members = User.objects.filter(role=User.Role.MEMBER)
        self.assertEqual(analytics.counts_by_bucket(members, "date_joined", period, analytics.DAY),
                         {date(2026, 3, 2): 1})
        section = analytics._memberships(period, analytics.DAY)
        self.assertEqual(cards(section)["New members"], 1)
        rows = section.charts[0]["rows"]
        self.assertEqual([row["values"][0] for row in rows], [0, 1, 0])
        self.assertEqual(rows[1]["label"], "2 Mar")

    def test_date_filter_narrows_the_figures(self):
        make_user("now@example.com")
        self.client.force_login(make_user("root@example.com", role=User.Role.SUPER_ADMIN))
        current = by_key(self.client.get(URL).context["sections"])["memberships"]
        past = by_key(self.client.get(URL, {"from": "2020-01-01", "to": "2020-01-31"}).context["sections"])["memberships"]
        self.assertEqual(cards(current)["New members"], 1)
        self.assertEqual(cards(past)["New members"], 0)
        self.assertEqual(cards(past)["Active members"], cards(current)["Active members"])  # "Today" figures stay.

    def test_a_bad_period_is_reported_and_the_default_is_shown(self):
        self.client.force_login(make_user("root@example.com", role=User.Role.SUPER_ADMIN))
        for params in ({"from": "nonsense"}, {"from": "2026-03-02", "to": "2026-03-01"},
                       {"from": "2024-01-01", "to": "2026-01-01"}):
            response = self.client.get(URL, params)
            with self.subTest(params=params):
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, "Showing the default period", status_code=400)

    def test_empty_period_shows_empty_states(self):
        self.client.force_login(make_user("root@example.com", role=User.Role.SUPER_ADMIN))
        response = self.client.get(URL, {"from": "2020-01-01", "to": "2020-01-31"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No activity in this period.")
        self.assertNotContains(response, "<rect class=\"analytics-bar--1\" x=")
        viewing = by_key(response.context["sections"])["viewing"]
        self.assertEqual(cards(viewing)["Verified rate"], "—")  # No division by zero.
        self.assertEqual(viewing.table["rows"], [])

    def test_chart_geometry_is_safe_in_every_language_and_uses_no_inline_css(self):
        make_user("now@example.com")
        self.client.force_login(make_user("root@example.com", role=User.Role.SUPER_ADMIN))
        for language in ("en", "sw"):
            with translation.override(language):
                html = self.client.get(URL, HTTP_ACCEPT_LANGUAGE=language).content.decode()
            with self.subTest(language=language):
                self.assertNotIn("style=", html)
                self.assertNotIn("<script>", html)
                self.assertNotRegex(html, r'(?:x|y|width|height)="\d+,\d+"')  # Localised decimals break SVG.
                self.assertRegex(html, r'viewBox="0 0 640 196"')
                self.assertIn("css/pages/administration/analytics.css", html)

    def test_viewing_the_dashboard_writes_nothing_and_is_bounded(self):
        make_user("now@example.com")
        root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        before = AuditEvent.objects.count()
        period = services.period_from()
        with CaptureQueriesContext(connection) as queries:
            analytics.dashboard(root, period)
        self.assertLessEqual(len(queries), 60)
        self.client.force_login(root)
        self.client.get(URL)
        self.assertEqual(AuditEvent.objects.count(), before)


class ViewingCorrectnessTests(RewardsTestCase):
    def setUp(self):
        super().setUp()
        self.approved_policy()
        self.verified_session()
        rewards.evaluate_pending()

    def dashboard(self, user=None):
        sections, financial = analytics.dashboard(user or self.root, services.period_from())
        return by_key(sections), by_key(financial)

    def test_viewing_equals_the_viewing_service(self):
        sections, _ = self.dashboard()
        figures = campaign_performance([self.campaign])[1][self.campaign.pk]
        viewing = cards(sections["viewing"])
        self.assertEqual((viewing["Sessions started"], viewing["Verified views"], viewing["Unique viewers"]),
                         (figures["sessions"], figures["verified"], figures["viewers"]))
        self.assertEqual(viewing["Verified rate"], "100%")
        chart = sections["viewing"].charts[0]
        self.assertEqual(sum(row["values"][1] for row in chart["rows"]), figures["verified"])
        self.assertEqual(sections["viewing"].table["rows"][0][:2], [self.campaign.name, self.org.display_name])

    def test_trend_totals_match_the_cards(self):
        sections, _ = self.dashboard()
        chart = sections["viewing"].charts[0]
        self.assertEqual(sum(row["values"][0] for row in chart["rows"]), cards(sections["viewing"])["Sessions started"])
        self.assertFalse(chart["empty"])
        self.assertGreater(len(chart["bars"]), 0)

    def test_funnel_shows_rewards_only_with_the_rewards_permission(self):
        with_rewards = staff_with("both@example.com", "view_advertising_report", "view_rewards_report")
        without = staff_with("ads@example.com", "view_advertising_report")
        funnel = self.dashboard(with_rewards)[0]["viewing"].breakdowns[0]["rows"]
        self.assertEqual([str(row["label"]) for row in funnel],
                         ["Sessions started", "Verified views", "Simulated rewards qualified"])
        self.assertEqual(funnel[2]["value"], 1)
        funnel = self.dashboard(without)[0]["viewing"].breakdowns[0]["rows"]
        self.assertEqual([str(row["label"]) for row in funnel], ["Sessions started", "Verified views"])

    def test_simulated_rewards_equal_the_rewards_totals(self):
        _, financial = self.dashboard()
        overview = admin_summary._rewards()
        figures = cards(financial["simulated"])
        self.assertEqual((figures["Qualified"], figures["Simulated amount (TZS)"]),
                         (overview["qualified"], overview["simulated_total"]))
        self.assertEqual(financial["simulated"].funds, "simulated")

    def test_page_exposes_no_member_identity(self):
        self.client.force_login(self.root)
        html = self.client.get(URL).content.decode()
        html = re.sub(r"<header.*?</header>", "", html, flags=re.S)  # The signed-in staff member's own menu.
        for private in (self.member.email, self.owner.email, str(self.member.pk)):
            self.assertNotIn(private, html)

    def test_advertisers_have_no_access_to_other_organizations_through_analytics(self):
        self.client.force_login(self.owner)  # An advertiser is not staff, whatever campaign they run.
        self.assertEqual(self.client.get(URL).status_code, 302)


class FinancialSeparationTests(PaymentsTestCase):
    def test_sandbox_figures_equal_the_payment_records(self):
        self.settled_deposit(50_000)
        self.deposit(20_000)
        financial = by_key(analytics.dashboard(self.root, services.period_from())[1])
        figures = cards(financial["sandbox"])
        self.assertEqual((figures["Payments created"], figures["Settled"], figures["Settled sandbox amount (TZS)"]),
                         (2, 1, 50_000))
        report = services.build("payments", services.period_from())
        settled = next(row for row in report.sections[0].rows if str(row[0]) == "Settled")
        self.assertEqual(settled[1:], [figures["Settled"], figures["Settled sandbox amount (TZS)"]])

    def test_every_financial_section_has_one_label_and_nothing_is_totalled(self):
        self.settled_deposit(50_000)
        self.client.force_login(self.root)
        response = self.client.get(URL)
        financial = response.context["financial"]
        funds = [section.funds for section in financial]
        self.assertEqual(sorted(funds), sorted(set(funds)))  # One section per funds type.
        self.assertEqual(set(funds), {"simulated", "sandbox", "demo", "budget", "real"})
        for label in ("Real money (locked, 0)", "Sandbox (test data, not money)", "Simulated demo rewards (not money)",
                      "Demo withdrawals (no money moves)", "Demo budget (not money)"):
            self.assertContains(response, label)
        for section in financial:
            for card in section.cards:
                self.assertNotIn("total", str(card.label).lower())
        for section in response.context["sections"]:
            self.assertEqual(section.funds, "")  # Operational sections carry no money-like figures.

    def test_real_money_stays_zero_after_sandbox_activity(self):
        self.settled_deposit(50_000)
        financial = by_key(analytics.dashboard(self.root, services.period_from())[1])
        figures = cards(financial["real"])
        self.assertEqual(figures["Campaigns with real funding"], 0)
        self.assertEqual(figures["Unexpected wallet balances"], 0)


class DemoWithdrawalTests(WithdrawalsTestCase):
    def test_demo_figures_equal_the_records(self):
        self.demo(50_000)
        financial = by_key(analytics.dashboard(self.root, services.period_from())[1])
        figures = cards(financial["demo"])
        self.assertEqual(figures["Requests"], 1)
        self.assertEqual(figures["Paid (simulated)"], 0)
        self.assertEqual(figures["Awaiting review"], admin_summary._withdrawals()["review_queue"])
        self.assertEqual(financial["demo"].funds, "demo")


REPORTING_ZONE = ZoneInfo("Africa/Dar_es_Salaam")


def reporting_today():
    """The date in Tanzania. It differs from the UTC date from 00:00 to 03:00 local time."""
    return timezone.now().astimezone(REPORTING_ZONE).date()


def new_members_in(first, last):
    period = services.period_from(first and first.isoformat(), last.isoformat())
    return cards(analytics._memberships(period, analytics.DAY))["New members"]


def new_members_on(day):
    return new_members_in(day, day)


class SupportAndOperationsTests(AccountsTestCase):
    def test_support_figures_and_trend(self):
        member = make_user("asker@example.com")
        ticket = support.create_ticket(member, subject="Cannot sign in", category="ACCOUNT",
                                       description="My password does not work.", submission_key=new_submission_key())
        period = services.period_from()
        section = analytics._support(period, analytics.grain_for(period))
        self.assertEqual(cards(section)["Tickets opened"], 1)
        self.assertEqual(cards(section)["Open tickets"], 1)
        self.assertEqual(cards(section)["Unassigned"], 1)
        self.assertEqual([str(row["label"]) for row in section.breakdowns[0]["rows"]], ["Account and sign-in"])
        self.assertNotIn(ticket.subject, str(section))

    def test_operations_counts_emails(self):
        from apps.notifications.models import OutboundEmail

        self.client.post(reverse("accounts:register"), {"email": "new@example.com"})
        period = services.period_from()
        section = analytics._operations(period, analytics.grain_for(period))
        self.assertEqual(cards(section)["Emails queued"], OutboundEmail.objects.count())
        self.assertNotIn("new@example.com", str(section))

    def test_period_today_is_included(self):
        make_user("today@example.com")
        self.assertEqual(new_members_in("", reporting_today()), 1)

    def test_reporting_day_follows_tanzania_not_utc(self):
        member = make_user("boundary@example.com")

        def utc(day, hour, minute=0, second=0):
            return datetime(2026, 11, day, hour, minute, second, tzinfo=dt_timezone.utc)

        cases = (  # Tanzanian time, the instant in UTC, its Tanzanian date, members in a period ending on the UTC date.
            ("23:59:59 on the 5th, last second before the window", utc(5, 20, 59, 59), date(2026, 11, 5), 1),
            ("00:00:00 on the 6th, the window opens", utc(5, 21), date(2026, 11, 6), 0),
            ("00:30 on the 6th, inside the window", utc(5, 21, 30), date(2026, 11, 6), 0),
            ("02:59:59 on the 6th, last second of the window", utc(5, 23, 59, 59), date(2026, 11, 6), 0),
            ("03:00:00 on the 6th, the window closes", utc(6, 0), date(2026, 11, 6), 1),
            ("12:00 on the 6th, well outside the window", utc(6, 9), date(2026, 11, 6), 1),
        )
        for label, instant, local_day, counted_by_utc_date in cases:
            with self.subTest(label), mock.patch("django.utils.timezone.now", return_value=instant):
                User.objects.filter(pk=member.pk).update(date_joined=instant)
                self.assertEqual(reporting_today(), local_day)
                self.assertEqual(services.period_from().last, local_day)
                self.assertEqual(new_members_in("", local_day), 1)
                self.assertEqual(new_members_in("", local_day - timedelta(days=1)), 0)
                self.assertEqual(new_members_on(local_day), 1)
                self.assertEqual(new_members_on(local_day - timedelta(days=1)), 0)
                self.assertEqual(new_members_on(local_day + timedelta(days=1)), 0)
                # Inside the window the UTC date is still yesterday, so a period ending on it misses this member.
                # This is the exact shape of the old failure.
                self.assertEqual(new_members_in("", instant.date()), counted_by_utc_date)
