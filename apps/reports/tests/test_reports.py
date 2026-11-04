"""Phase 15-C: reports (permissions, correctness, money separation, privacy, CSV export)."""

import csv
import io
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.helpers import AccountsTestCase, User, make_user
from apps.administration import summary as admin_summary
from apps.advertising import services as advertising
from apps.audit.models import AuditEvent
from apps.core.models import SiteConfiguration
from apps.payments import services as payments
from apps.payments.tests.helpers import PaymentsTestCase
from apps.reports import services
from apps.rewards import services as rewards
from apps.rewards.tests.helpers import RewardsTestCase
from apps.support import services as support
from apps.support.forms import new_submission_key
from apps.viewing.services import campaign_performance
from apps.withdrawals.tests.helpers import WithdrawalsTestCase

SLUGS = ("members", "advertising", "rewards", "payments", "withdrawals", "operations", "audit", "support")
HIDDEN = "fewer than 5"


def grant(user, *codenames):
    user.user_permissions.add(*Permission.objects.filter(content_type__app_label="reports", codename__in=codenames))
    return User.objects.get(pk=user.pk)


def section(report, title):
    return next(s for s in report.sections if str(s.title) == title)


def rows_of(report, title):
    return {str(row[0]): row[1:] for row in section(report, title).rows}


def read_csv(response):
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))


class PermissionTests(AccountsTestCase):
    def setUp(self):
        super().setUp()
        self.staff = make_user("staff@example.com", role=User.Role.CONTENT_MANAGER)
        self.member = make_user("member@example.com")

    def test_each_report_needs_its_own_permission(self):
        for slug in SLUGS:
            viewer = grant(make_user(f"{slug}@example.com", role=User.Role.CONTENT_MANAGER), f"view_{slug}_report")
            self.client.force_login(viewer)
            for other in SLUGS:
                with self.subTest(viewer=slug, report=other):
                    response = self.client.get(reverse("administration:report", args=[other]))
                    self.assertEqual(response.status_code, 200 if other == slug else 403)

    def test_super_administrator_sees_all_eight(self):
        self.client.force_login(make_user("root@example.com", role=User.Role.SUPER_ADMIN))
        response = self.client.get(reverse("administration:reports"))
        self.assertEqual(len(response.context["reports"]), 8)
        for slug in SLUGS:
            self.assertEqual(self.client.get(reverse("administration:report", args=[slug])).status_code, 200)

    def test_no_permission_no_access_and_no_menu_entry(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("administration:reports")).status_code, 403)
        link = f'href="{reverse("administration:reports")}"'
        self.assertNotContains(self.client.get(reverse("administration:overview")), link)
        viewer = grant(self.staff, "view_support_report")
        self.client.force_login(viewer)
        self.assertContains(self.client.get(reverse("administration:overview")), link)
        listing = self.client.get(reverse("administration:reports"))
        self.assertEqual([r.slug for r in listing.context["reports"]], ["support"])

    def test_members_and_anonymous_are_kept_out(self):
        url = reverse("administration:report", args=["members"])
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(url).status_code, 302)  # To the admin login.
        self.assertEqual(self.client.post(reverse("administration:report_export", args=["members"])).status_code, 302)

    def test_unknown_report_is_not_found(self):
        self.client.force_login(make_user("root@example.com", role=User.Role.SUPER_ADMIN))
        self.assertEqual(self.client.get(reverse("administration:report", args=["ledger"])).status_code, 404)
        self.assertEqual(self.client.post(reverse("administration:report_export", args=["ledger"])).status_code, 404)

    def test_viewing_writes_nothing_and_is_bounded(self):
        root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.client.force_login(root)
        before = AuditEvent.objects.count()
        for slug in SLUGS:
            self.client.get(reverse("administration:report", args=[slug]))
        self.assertEqual(AuditEvent.objects.count(), before)
        period = services.period_from()
        for slug in SLUGS:
            with self.subTest(report=slug), CaptureQueriesContext(connection) as queries:
                services.build(slug, period)
            self.assertLessEqual(len(queries), 15)


class PeriodTests(AccountsTestCase):
    def test_default_is_the_last_thirty_days_and_bad_input_is_refused(self):
        period = services.period_from()
        self.assertEqual((period.last - period.first).days, 29)
        for first, last in [("nonsense", ""), ("2026-03-02", "2026-03-01"), ("2024-01-01", "2026-01-01")]:
            with self.subTest(first=first, last=last), self.assertRaises(services.PeriodError):
                services.period_from(first, last)

    def test_day_boundaries_follow_the_site_time_zone(self):
        SiteConfiguration.objects.filter(id=1).update(default_timezone="Africa/Dar_es_Salaam")
        cache.clear()
        member = make_user("night@example.com")
        # 21:30 UTC on 1 March is 00:30 on 2 March in Dar es Salaam.
        User.objects.filter(pk=member.pk).update(date_joined=datetime(2026, 3, 1, 21, 30, tzinfo=dt_timezone.utc))

        def registered(day):
            report = services.build("members", services.period_from(day, day))
            return rows_of(report, "Members who registered")["Registered"][0]

        self.assertEqual(registered("2026-03-01"), 0)
        self.assertEqual(registered("2026-03-02"), 1)

    def test_a_bad_period_on_the_page_is_reported(self):
        self.client.force_login(make_user("root@example.com", role=User.Role.SUPER_ADMIN))
        response = self.client.get(reverse("administration:report", args=["members"]), {"from": "nonsense"})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "YYYY-MM-DD", status_code=400)


class RewardsAndAdvertisingCorrectnessTests(RewardsTestCase):
    def setUp(self):
        super().setUp()
        self.approved_policy()
        self.verified_session()
        rewards.evaluate_pending()
        self.period = services.period_from()

    def test_simulated_rewards_equal_the_rewards_service(self):
        report = services.build("rewards", self.period)
        expected = next(iter(rewards.campaign_totals([self.campaign])))
        row = section(report, "Demo rewards by campaign").rows[0]
        self.assertEqual(row, [self.campaign.name, expected["qualified"], expected["reversed"], expected["rejected"],
                               expected["demo_consumed"]])
        overview = admin_summary._rewards()
        totals = rows_of(report, "Demo reward decisions")
        self.assertEqual(totals["Qualified"], [overview["qualified"]])
        self.assertEqual(totals["Simulated amount, qualified (TZS)"], [overview["simulated_total"]])

    def test_viewing_equals_the_viewing_service_and_staff_see_exact_counts(self):
        report = services.build("advertising", self.period)
        _, by_campaign = campaign_performance([self.campaign])
        figures = by_campaign[self.campaign.pk]
        row = section(report, "Viewing by campaign").rows[0]
        self.assertEqual(row, [self.campaign.name, self.org.display_name, figures["sessions"], figures["verified"],
                               figures["viewers"], figures["in_review"], figures["not_verified"]])
        self.assertEqual(figures["verified"], 1)
        outcomes = rows_of(report, "Viewing sessions, by outcome")
        self.assertEqual(sum(value[0] for value in outcomes.values()), admin_summary._viewing()["outcomes"]
                         .get("VERIFIED_EVIDENCE", 0))

    def test_records_outside_the_period_are_left_out(self):
        past = services.period_from("2020-01-01", "2020-01-31")
        self.assertEqual(rows_of(services.build("rewards", past), "Demo reward decisions")["Qualified"], [0])
        self.assertEqual(section(services.build("advertising", past), "Viewing by campaign").rows, [])

    def test_demo_budget_and_real_funding_are_separate_and_labelled(self):
        report = services.build("advertising", self.period)
        budget = section(report, "Demo budget of campaigns created (TZS)")
        self.assertEqual(budget.funds, "budget")
        self.assertEqual(str(budget.funds_label), "Demo budget (not money)")
        real = section(report, "Real campaign funding")
        self.assertEqual((real.funds, real.rows[0][1]), ("real", 0))
        self.assertEqual(str(real.funds_label), "Real money (locked, 0)")


class AdvertiserPrivacyTests(RewardsTestCase):
    def setUp(self):
        super().setUp()
        self.approved_policy()
        self.verified_session()
        rewards.evaluate_pending()
        self.url = reverse("advertising:reports", args=[self.org.pk])

    def test_the_approved_threshold_is_the_default(self):
        self.assertEqual(settings.ADVERTISING_REPORT_MIN_COUNT, 5)

    def test_small_counts_are_masked_on_the_advertiser_report(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.url)
        report = response.context["report"]
        self.assertEqual(report["totals"]["verified"], HIDDEN)
        self.assertEqual(report["totals"]["viewers"], HIDDEN)
        row = report["rows"][0]
        self.assertEqual((row["verified"], row["viewers"], row["qualified"]), (HIDDEN, HIDDEN, HIDDEN))
        self.assertEqual(row["in_review"], "0")  # Zero identifies nobody.
        self.assertContains(response, HIDDEN)

    def test_counts_at_or_above_the_threshold_are_exact(self):
        self.client.force_login(self.owner)
        with override_settings(ADVERTISING_REPORT_MIN_COUNT=1):
            self.assertEqual(self.client.get(self.url).context["report"]["totals"]["verified"], "1")

    def test_campaign_page_hides_the_amount_a_small_count_could_be_derived_from(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("advertising:campaign", args=[self.org.pk, self.campaign.pk]))
        performance = response.context["performance"]
        self.assertEqual(performance["viewing"]["viewers"]["display"], HIDDEN)
        self.assertIsNone(performance["demo_rewards"]["demo_consumed"])
        self.assertIsNone(performance["demo_rewards"]["demo_remaining"])
        self.assertContains(response, "Hidden for privacy")
        self.assertNotContains(response, "TZS 100 /")
        overview = self.client.get(reverse("advertising:organization", args=[self.org.pk]))
        self.assertEqual(overview.context["report"]["viewing"]["verified"]["display"], HIDDEN)

    def test_staff_campaign_page_keeps_exact_figures(self):
        staff = advertising.campaign_report(self.campaign, for_advertiser=False)
        self.assertEqual(staff["viewing"]["verified"]["value"], 1)
        self.assertEqual(staff["demo_rewards"]["demo_consumed"], 100)

    def test_other_organizations_are_not_found_and_their_campaigns_never_appear(self):
        outsider = self.new_member("outsider@example.com")
        other = self.make_org(outsider, "Other brand")
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        response = self.client.get(reverse("advertising:reports", args=[other.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["report"]["rows"], [])
        self.assertEqual(response.context["report"]["totals"]["verified"], "0")
        self.assertNotContains(response, self.campaign.name)

    def test_advertisers_reach_reports_from_their_tabs_and_cannot_export(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("advertising:organization", args=[self.org.pk])),
                            f'href="{self.url}"')
        response = self.client.post(reverse("administration:report_export", args=["advertising"]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(AuditEvent.objects.filter(action="reports.exported").exists())


class ExportTests(RewardsTestCase):
    def setUp(self):
        super().setUp()
        self.approved_policy()
        self.verified_session()
        rewards.evaluate_pending()
        self.exporter = grant(make_user("exporter@example.com", role=User.Role.CONTENT_MANAGER),
                              "view_advertising_report", "view_rewards_report", "export_reports")
        self.url = reverse("administration:report_export", args=["advertising"])

    def test_export_needs_both_the_report_and_the_export_permission(self):
        viewer = grant(make_user("viewer@example.com", role=User.Role.CONTENT_MANAGER), "view_advertising_report")
        exporter_only = grant(make_user("only@example.com", role=User.Role.CONTENT_MANAGER), "export_reports")
        for user in (viewer, exporter_only):
            self.client.force_login(user)
            with self.subTest(user=user.email):
                self.assertEqual(self.client.post(self.url).status_code, 403)
        denied = AuditEvent.objects.filter(action="reports.action.denied")
        self.assertEqual(denied.count(), 2)
        self.assertEqual(denied.first().metadata["report"], "advertising")
        self.assertFalse(AuditEvent.objects.filter(action="reports.exported").exists())

    def test_the_export_button_only_shows_with_permission(self):
        page = reverse("administration:report", args=["advertising"])
        viewer = grant(make_user("viewer@example.com", role=User.Role.CONTENT_MANAGER), "view_advertising_report")
        self.client.force_login(viewer)
        self.assertNotContains(self.client.get(page), "Export CSV")
        self.client.force_login(self.exporter)
        self.assertContains(self.client.get(page), "Export CSV")

    def test_export_is_a_post_with_csrf_and_is_audited(self):
        self.client.force_login(self.exporter)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.exporter)
        self.assertEqual(strict.post(self.url).status_code, 403)
        self.assertFalse(AuditEvent.objects.filter(action="reports.exported").exists())

        response = self.client.post(self.url, {"from": "", "to": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn('attachment; filename="clipcash-advertising-', response["Content-Disposition"])
        self.assertEqual(response["Cache-Control"], "no-store")
        event = AuditEvent.objects.get(action="reports.exported")
        self.assertEqual((event.category, event.actor_id), ("REPORTS", self.exporter.pk))
        self.assertEqual(event.metadata["report"], "advertising")
        self.assertEqual(event.metadata["rows"], len(read_csv(response)))

    def test_export_masks_small_campaign_counts_but_the_staff_page_is_exact(self):
        self.client.force_login(self.exporter)
        rows = read_csv(self.client.post(self.url))
        viewers = [r for r in rows if r["Section"] == "Viewing by campaign" and r["Measure"] == "Unique viewers"]
        self.assertEqual(len(viewers), 1)
        self.assertEqual(viewers[0]["Value"], HIDDEN)
        self.assertEqual(viewers[0]["Row"], f"{self.campaign.name} / {self.org.display_name}")
        screen = self.client.get(reverse("administration:report", args=["advertising"])).context["report"]
        self.assertEqual(section(screen, "Viewing by campaign").rows[0][4], 1)

    def test_rewards_export_hides_the_amount_with_the_count(self):
        self.client.force_login(self.exporter)
        rows = read_csv(self.client.post(reverse("administration:report_export", args=["rewards"])))
        campaign = {r["Measure"]: r["Value"] for r in rows if r["Section"] == "Demo rewards by campaign"}
        self.assertEqual(campaign["Qualified"], HIDDEN)
        self.assertEqual(campaign["Simulated amount (TZS)"], HIDDEN)

    def test_exports_contain_no_personal_data(self):
        root = make_user("second-root@example.com", role=User.Role.SUPER_ADMIN)
        self.client.force_login(root)
        emails = [self.member.email, self.owner.email, root.email, self.root.email]
        for slug in SLUGS:
            content = self.client.post(reverse("administration:report_export", args=[slug])).content.decode("utf-8-sig")
            for email in emails:
                with self.subTest(report=slug, email=email):
                    self.assertNotIn(email, content)
            self.assertNotIn(str(self.member.pk), content)

    def test_spreadsheet_formulas_in_names_are_neutralised(self):
        self.assertEqual(services._cell("=HYPERLINK(1)"), "'=HYPERLINK(1)")
        self.assertEqual(services._cell("@cmd"), "'@cmd")
        self.assertEqual(services._cell("-1+1"), "'-1+1")
        self.assertEqual(services._cell("Launch"), "Launch")
        self.assertEqual(services._cell(-5), -5)  # Numbers stay numbers.

    def test_export_of_a_bad_period_exports_nothing(self):
        self.client.force_login(self.exporter)
        self.assertEqual(self.client.post(self.url, {"from": "nonsense"}).status_code, 302)
        self.assertFalse(AuditEvent.objects.filter(action="reports.exported").exists())

    def test_exports_are_rate_limited(self):
        self.client.force_login(self.exporter)
        for _ in range(20):
            self.assertEqual(self.client.post(self.url).status_code, 200)
        self.assertEqual(self.client.post(self.url).status_code, 302)
        self.assertEqual(AuditEvent.objects.filter(action="reports.exported").count(), 20)
        self.assertEqual(AuditEvent.objects.filter(action="reports.action.denied").count(), 1)


class SandboxPaymentReportTests(PaymentsTestCase):
    def test_figures_match_the_payment_records_and_balances_are_the_services_own(self):
        self.settled_deposit(50_000)
        self.deposit(20_000)
        report = services.build("payments", services.period_from())
        statuses = rows_of(report, "Sandbox payments, by status")
        self.assertEqual(statuses["Settled"], [1, 50_000])
        self.assertEqual(sum(row[0] for row in statuses.values()), 2)
        balances = section(report, "Sandbox journal balances (debit minus credit)")
        expected = [[account, scope or "platform", balance]
                    for (account, scope), balance in payments.sandbox_balances().items()]
        self.assertEqual([[str(row[0]), str(row[1]), row[2]] for row in balances.rows], expected)
        self.assertTrue(balances.as_of_now)

    def test_sections_are_labelled_and_never_mix_funds(self):
        self.settled_deposit()
        report = services.build("payments", services.period_from())
        self.assertEqual({s.funds for s in report.sections}, {"sandbox", "real"})
        for current in report.sections:
            with self.subTest(section=str(current.title)):
                self.assertTrue(current.funds_label)
                self.assertNotIn("total", " ".join(str(c).lower() for c in current.columns))
        real = section(report, "Real wallets")
        self.assertEqual(rows_of(report, "Real wallets")["Unexpected balances"], [0])
        self.assertEqual(real.funds, "real")


class DemoWithdrawalReportTests(WithdrawalsTestCase):
    def test_figures_match_the_records_and_are_labelled_demo(self):
        self.demo(50_000)
        report = services.build("withdrawals", services.period_from())
        self.assertEqual({s.funds for s in report.sections}, {"demo"})
        statuses = rows_of(report, "Demo withdrawals, by status")
        self.assertEqual(sum(row[0] for row in statuses.values()), 1)
        self.assertEqual(sum(row[1] for row in statuses.values()), 50_000)
        queues = rows_of(report, "Demo withdrawal queues today")
        self.assertEqual(queues["Awaiting review"], [admin_summary._withdrawals()["review_queue"]])

    def test_the_real_money_lock_is_untouched_by_reading_reports(self):
        before = payments.sandbox_balances()
        self.demo(50_000)
        services.build("withdrawals", services.period_from())
        self.assertEqual(payments.sandbox_balances(), before)


class SupportAuditOperationsReportTests(AccountsTestCase):
    def test_support_report_counts_tickets_without_identities(self):
        member = make_user("asker@example.com")
        ticket = support.create_ticket(member, subject="Cannot sign in", category="ACCOUNT",
                                       description="My password does not work.", submission_key=new_submission_key())
        agent = make_user("agent@example.com", role=User.Role.CONTENT_MANAGER)
        agent.user_permissions.add(Permission.objects.get(content_type__app_label="support", codename="manage_tickets"))
        agent = User.objects.get(pk=agent.pk)
        support.staff_reply(agent, ticket, body="SECRET_NOTE_TEXT", internal=True, submission_key=new_submission_key())
        report = services.build("support", services.period_from())
        self.assertEqual(rows_of(report, "Tickets opened, by status")["Open"], [1])
        self.assertEqual(rows_of(report, "Tickets opened, by category")["Account and sign-in"], [1])
        activity = rows_of(report, "Activity")
        self.assertEqual((activity["Customer messages"], activity["Internal notes"]), ([1], [1]))
        self.assertEqual(rows_of(report, "Open tickets today")["Unassigned"], [1])
        flat = str([s.rows for s in report.sections])
        for private in ("asker@example.com", "agent@example.com", "SECRET_NOTE_TEXT", "Cannot sign in"):
            self.assertNotIn(private, flat)

    def test_audit_report_counts_denials_without_identities(self):
        make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        member = make_user("denied@example.com")
        services.deny(member, "export", "members")
        report = services.build("audit", services.period_from())
        self.assertEqual(rows_of(report, "Denied actions, by category")["Reports"], [1])
        self.assertEqual(rows_of(report, "Most denied actions")["reports.action.denied"], [1])
        self.assertNotIn("denied@example.com", str([s.rows for s in report.sections]))

    def test_operations_report_counts_emails(self):
        from apps.notifications.models import OutboundEmail

        self.client.post(reverse("accounts:register"), {"email": "new@example.com"})
        report = services.build("operations", services.period_from())
        emails = rows_of(report, "Emails, by kind")
        self.assertEqual(emails["accounts.activation"], [OutboundEmail.objects.filter(kind="accounts.activation").count()])
        self.assertNotIn("new@example.com", str([s.rows for s in report.sections]))

    def test_members_report_reconciles_with_the_overview(self):
        make_user("one@example.com")
        make_user("two@example.com", verified=False)
        report = services.build("members", services.period_from())
        overview = admin_summary._members()
        self.assertEqual(rows_of(report, "Members who registered")["Registered"], [overview["total"]])
        self.assertEqual(rows_of(report, "Members who registered")["Email confirmed"], [overview["verified"]])
        self.assertEqual(rows_of(report, "All members today")["Members"], [overview["total"]])
