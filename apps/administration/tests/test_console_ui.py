"""UI-07: the administration console stays permission-aware, shows readable statuses and keeps money separated."""

import re

from django.conf import settings
from django.contrib.auth.models import Permission
from django.template import Context, Template
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from apps.accounts.tests.helpers import make_user
from apps.administration import summary
from apps.administration.labels import CODE_LABELS, code_label
from apps.administration.views import VIEWING_FLAGS
from apps.advertising import services as advertising
from apps.payments import services as payments, simulator
from apps.payments.tests.helpers import token
from apps.rewards.tests.helpers import RewardsTestCase, User
from apps.support import services as support
from apps.support.forms import new_submission_key
from apps.wallets import wallet_services
from apps.wallets.models import WalletDiscrepancy
from apps.withdrawals import services as withdrawals
from apps.withdrawals.models import PayoutBeneficiary

TEMPLATES = settings.BASE_DIR / "templates/administration"
CSS = settings.BASE_DIR / "static/css"
SANDBOX = override_settings(PAYMENTS_MODE="sandbox", PAYMENTS_SANDBOX_ALLOWED=True)
OVERVIEW = reverse("administration:overview")


def console(name, *args):
    return reverse(f"administration:{name}", args=args)


class LabelTests(SimpleTestCase):
    def test_known_codes_have_written_labels_and_unknown_codes_stay_readable(self):
        self.assertEqual(str(code_label("worker_heartbeat_stale")), "Background worker has not reported recently")
        self.assertEqual(code_label("wallets.access.denied"), "Wallets: access denied")
        self.assertEqual(code_label("DEMO_UNDER_REVIEW"), "Demo under review")
        self.assertEqual(code_label("some_new_code"), "Some new code")
        self.assertEqual(code_label(""), "")
        self.assertEqual(code_label(None), "")

    def test_every_viewing_flag_and_job_problem_has_a_written_label(self):
        for code in (*VIEWING_FLAGS, "worker_heartbeat_stale", "queue_backlog", "expired_leases", "cache_unavailable"):
            with self.subTest(code=code):
                if code in CODE_LABELS:
                    self.assertNotIn("_", str(code_label(code)))
                else:
                    self.assertEqual(str(code_label(code)), code.replace("_", " ").capitalize())

    def test_grouped_formats_whole_numbers_only(self):
        html = Template('{% load console_labels %}{{ a|grouped }}|{{ b|grouped }}').render(
            Context({"a": 75000000, "b": "100%"}))
        self.assertEqual(html, "75,000,000|100%")

    def test_template_filters(self):
        html = Template('{% load console_labels %}{{ codes|code_labels }}|{{ code|code_label }}').render(
            Context({"codes": ["large_seeks", "queue_backlog"], "code": "impossible_speed"}))
        self.assertEqual(html, "Large jumps in the video, Job queue is backing up|Impossible playback speed")


class TemplateIntegrationTests(SimpleTestCase):
    def sources(self):
        return {path.relative_to(TEMPLATES).as_posix(): path.read_text(encoding="utf-8")
                for path in TEMPLATES.rglob("*.html")}

    def test_every_console_table_has_a_caption_and_column_scopes(self):
        for name, source in self.sources().items():
            for table in re.findall(r"<table\b.*?</table>", source, flags=re.S):
                with self.subTest(template=name):
                    self.assertIn("<caption", table)
                    self.assertNotRegex(table, r"<th(?=[\s>])(?![^>]*scope=)")

    def test_no_inline_css_raw_amounts_or_leftover_conditions(self):
        for name, source in self.sources().items():
            with self.subTest(template=name):
                self.assertNotRegex(source, r"\bstyle=|<style")
                self.assertNotRegex(source, r"TZS \{\{")
                self.assertNotIn("{% if True %}", source)

    def test_cards_remain_only_for_the_separated_financial_categories(self):
        for name, source in self.sources().items():
            with self.subTest(template=name):
                self.assertNotRegex(source, r'class="[^"]*\b(summary-card|summary-grid|stat-grid|card-grid)\b')
                if name != "analytics/dashboard.html":
                    self.assertNotRegex(source, r'class="panel\b')

    def test_shared_patterns_live_in_components_and_no_giant_admin_stylesheet(self):
        self.assertTrue((CSS / "components/workspace.css").exists())
        self.assertTrue((CSS / "components/settings-groups.css").exists())
        self.assertFalse((CSS / "pages/advertising/settings.css").exists())
        self.assertLess(len((CSS / "layouts/admin.css").read_text(encoding="utf-8").splitlines()), 120)


class ConsoleTestCase(RewardsTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.root)

    def staff_with(self, email, *codenames):
        user = make_user(email, role=User.Role.CONTENT_MANAGER)
        user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
        self.client.force_login(user)
        return user


class OverviewTests(ConsoleTestCase):
    def test_summary_is_a_metric_row_and_tables_not_cards(self):
        page = self.client.get(OVERVIEW)
        self.assertContains(page, 'class="metric-row"')
        self.assertContains(page, "Needs attention")
        self.assertContains(page, "By area")
        self.assertNotContains(page, "summary-card")

    def test_attention_lists_real_open_work_with_working_links(self):
        submitted = advertising.submit_campaign(self.owner, self.make_campaign(name="Waiting"))
        self.assertEqual(submitted.status, "SUBMITTED")
        page = self.client.get(OVERVIEW)
        items = {str(item["label"]): item for item in page.context["attention"]}
        self.assertEqual(items["Campaigns awaiting review"]["count"], 1)
        self.assertContains(page, console("campaigns"))
        for item in page.context["attention"]:
            with self.subTest(item=str(item["label"])):
                self.assertEqual(self.client.get(reverse(item["url_name"]) + item["query"]).status_code, 200)

    def test_attention_is_built_only_from_permitted_sections(self):
        self.assertEqual(summary.attention_items({}), [])
        advertising.submit_campaign(self.owner, self.make_campaign(name="Waiting"))
        self.staff_with("content@x.example")
        page = self.client.get(OVERVIEW)
        self.assertNotContains(page, "Campaigns awaiting review")
        self.assertNotContains(page, "Demo rewards")

    @SANDBOX
    def test_financial_categories_stay_named_and_are_never_summed(self):
        page = self.client.get(OVERVIEW)
        for text in ("simulated, not money", "Not real cash", "Every balance must be TZS 0",
                     "Real withdrawals are locked", "Wallets (real ledger)"):
            self.assertContains(page, text)
        self.assertNotContains(page, "Total revenue")
        self.assertNotContains(page, "Total balance")

    def test_job_problems_are_readable(self):
        page = self.client.get(OVERVIEW)
        self.assertNotContains(page, "worker_heartbeat_stale")
        self.assertNotContains(page, "queue_backlog")
        health = self.client.get(console("health"))
        self.assertNotContains(health, "worker_heartbeat_stale")


class NavigationTests(ConsoleTestCase):
    def test_personal_links_form_their_own_group_with_one_entry_each(self):
        page = self.client.get(OVERVIEW)
        self.assertContains(page, ">Your account</p>")
        html = page.content.decode()
        menu = html.split('id="console-menu"', 1)[1].split("</nav>", 1)[0]
        self.assertEqual(menu.count(f'href="{reverse("accounts:profile")}"'), 1)
        self.assertEqual(menu.count(f'href="{reverse("notifications:inbox")}"'), 1)

    def test_active_entry_follows_the_page(self):
        self.assertContains(self.client.get(console("campaigns")), f'aria-current="page" href="{console("campaigns")}"')

    def test_navigation_hides_what_a_role_cannot_open(self):
        self.staff_with("agent@x.example", "view_tickets")
        page = self.client.get(OVERVIEW)
        self.assertContains(page, console("support"))
        for hidden in ("Member directory", "Financial simulation", "Security summary"):
            self.assertNotContains(page, hidden)
        self.assertEqual(self.client.get(console("members")).status_code, 403)
        self.assertEqual(self.client.get(console("security")).status_code, 403)


@SANDBOX
class WorkflowPageTests(ConsoleTestCase):
    def test_lists_use_workspace_tables_with_captions(self):
        self.make_campaign()
        for name in ("members", "advertisers", "campaigns", "videos", "categories", "viewing", "demo_rewards",
                     "reward_policies", "wallets", "withdrawals", "support", "reports", "health"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(console(name)).status_code, 200)

    def test_review_forms_keep_their_safeguards(self):
        campaign = advertising.submit_campaign(self.owner, self.make_campaign(name="Waiting"))
        page = self.client.get(console("campaign_review", campaign.pk))
        for text in (f'name="expected_revision" value="{campaign.revision}"', "csrfmiddlewaretoken",
                     "Cancelling is permanent", "Request changes", "Demonstration budgets only"):
            self.assertContains(page, text)
        self.client.post(console("campaign_review", campaign.pk), {"action": "approve", "expected_revision": "999"})
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "SUBMITTED")

    def test_campaign_page_reports_exact_figures_with_demo_labels(self):
        page = self.client.get(console("campaign_review", self.campaign.pk))
        self.assertContains(page, "Verified views")
        self.assertContains(page, "Demo rewards (simulated, not money)")
        self.assertContains(page, "TZS 100,000")
        self.assertContains(page, "Spend: not applicable")

    def test_viewing_flags_read_as_words_but_filter_values_are_unchanged(self):
        page = self.client.get(console("viewing"))
        self.assertContains(page, '<option value="large_seeks">Large jumps in the video</option>', html=True)
        self.assertNotContains(page, ">large_seeks<")

    def test_wallet_findings_and_amounts_are_readable(self):
        wallet = wallet_services.ensure_wallet(self.member)
        WalletDiscrepancy.objects.create(wallet=wallet, code="unexpected_balance", detail="Sample.")
        detail = self.client.get(console("wallet", wallet.pk))
        self.assertContains(detail, "Balance should be zero")
        self.assertNotContains(detail, "unexpected_balance")
        self.assertContains(detail, "TZS 0")
        self.assertContains(detail, "There are no balance controls")
        self.assertContains(self.client.get(console("wallet_reconciliation")), "Balance should be zero")

    def test_demo_withdrawal_history_uses_words_and_keeps_stored_values_in_the_form(self):
        beneficiary = withdrawals.add_beneficiary(self.member, kind=PayoutBeneficiary.Kind.MOBILE_MONEY, network="MPESA",
                                                  identifier="0712 345 678", holder_name="Asha Juma")
        demo = withdrawals.create_demo_withdrawal(self.root, self.member, beneficiary, amount=50_000,
                                                  idempotency_key=token())
        page = self.client.get(console("withdrawal", demo.pk))
        self.assertContains(page, "DEMO / SIMULATED")
        self.assertContains(page, "TZS 50,000")
        self.assertContains(page, 'class="badge badge--demo_requested"')
        self.assertContains(page, '<option value="suspected_fraud">Suspected fraud</option>', html=True)
        visible = re.sub(r"<[^>]+>", " ", page.content.decode())
        self.assertNotIn("DEMO_REQUESTED", visible)
        self.assertNotIn("0712345678", visible)

    def test_sandbox_payment_page_keeps_labels_and_valid_markup(self):
        deposit = payments.create_deposit_intent(self.owner, self.org, amount=2_500_000, idempotency_key=token())
        simulator.simulate(self.root, deposit, "pay")
        simulator.simulate(self.root, deposit, "settle")
        page = self.client.get(console("payment", deposit.pk))
        self.assertContains(page, "TZS 2,500,000")
        self.assertContains(page, "Sandbox")
        self.assertContains(page, "Sandbox provider simulator")
        self.assertEqual(self.client.get(console("payment_funding")).status_code, 200)
        self.assertContains(self.client.get(console("payment_funding")), "TZS 2,500,000")
        source = (TEMPLATES / "payments/detail.html").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"\{% for \w+ in (refunds|disputes) %\}<p>")

    @override_settings(RESTRICTED_DEMO_MODE=True, MEMBERSHIPS_COMPLIMENTARY_GRANTS_ENABLED=True,
                       PAYMENTS_MODE="disabled", PAYMENTS_SANDBOX_ALLOWED=False)
    def test_complimentary_grant_page_renders_with_its_warning(self):
        member = make_user("grantee@x.example")
        page = self.client.get(console("member_complimentary", member.pk))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "It is not a payment")
        self.assertContains(page, "Review grant")


class SupportTests(ConsoleTestCase):
    def setUp(self):
        super().setUp()
        self.ticket = support.create_ticket(self.member, subject="Cannot see my video", category="VIDEOS",
                                            description="The player stays black on my phone.",
                                            submission_key=new_submission_key())
        support.staff_reply(self.root, self.ticket, body="Public answer.", internal=False,
                            submission_key=new_submission_key())
        support.staff_reply(self.root, self.ticket, body="Private staff note.", internal=True,
                            submission_key=new_submission_key())

    def test_staff_see_internal_notes_marked_and_customers_never_see_them(self):
        page = self.client.get(console("support_ticket", self.ticket.reference))
        self.assertContains(page, "support-note--internal")
        self.assertContains(page, "Private staff note.")
        self.client.force_login(self.member)
        customer = self.client.get(reverse("support:ticket", args=[self.ticket.reference]))
        self.assertContains(customer, "Public answer.")
        self.assertNotContains(customer, "Private staff note.")
        self.assertNotContains(customer, "support-note--internal")

    def test_view_only_staff_get_no_management_forms(self):
        self.staff_with("viewer@x.example", "view_tickets")
        page = self.client.get(console("support_ticket", self.ticket.reference))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Reply or add an internal note")
        self.assertNotContains(page, 'name="action" value="assign"')

    def test_queue_filter_empty_state(self):
        self.assertContains(self.client.get(console("support"), {"q": "zzzz"}), "No tickets match these filters")

    def test_staff_inbox_still_renders(self):
        self.assertEqual(self.client.get(reverse("notifications:inbox")).status_code, 200)


class ReportingTests(ConsoleTestCase):
    def test_analytics_uses_metric_rows_and_keeps_financial_categories_apart(self):
        page = self.client.get(console("analytics"))
        self.assertContains(page, 'class="metric-row"')
        self.assertContains(page, "never added together")
        self.assertNotContains(page, "stat-grid")
        self.assertContains(page, "pages/administration/analytics.css")

    def test_report_period_and_export_are_preserved(self):
        page = self.client.get(console("report", "members"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'class="period-field"')
        self.assertContains(page, "Export CSV")
        self.assertContains(page, "workspace-table")

    def test_report_permission_still_applies(self):
        self.staff_with("reporter@x.example", "view_members_report")
        self.assertEqual(self.client.get(console("report", "members")).status_code, 200)
        self.assertEqual(self.client.get(console("report", "rewards")).status_code, 403)
        self.assertNotContains(self.client.get(console("report", "members")), "Export CSV")
