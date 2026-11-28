"""OPS-01: business dashboard (real figures, permission-limited, working task links, separate money
lines), settings landing page, System health job detail, account header without internal IDs, testing
tools hidden from normal member pages, and the member payments page."""

import re

from django.test import override_settings
from django.urls import reverse

from apps.administration import navigation
from apps.advertising.tests.helpers import AdvertisingTestCase, User, make_user
from apps.payments import manual
from apps.payments.tests.manual_helpers import MANUAL_ON, staff_with

OVERVIEW = reverse("administration:overview")


@MANUAL_ON
class DashboardTests(AdvertisingTestCase):
    def setUp(self):
        super().setUp()
        from apps.memberships import services as memberships

        manual.publish_instruction(self.root, business_number="13579246", recipient_name="CLIPCASH TEST LTD")
        self.subscription = memberships.choose_plan(self.member, self.plan)
        self.claim, _ = manual.submit_claim(self.member, self.subscription, amount=self.subscription.price_amount,
                                            payer_phone="0781234567", transaction_reference="MPOPS1234567")

    def test_super_admin_dashboard_shows_real_figures_tasks_and_quick_actions(self):
        self.client.force_login(self.root)
        page = self.client.get(OVERVIEW)
        stats = {str(s["label"]): s for s in page.context["stats"]}
        self.assertEqual(stats["Members"]["value"], f"{User.objects.filter(role=User.Role.MEMBER).count():,}")
        self.assertEqual(stats["Confirmed payments"]["value"], "0")
        tasks = {str(t.label): t for t in page.context["tasks"]}
        self.assertEqual(tasks["Membership payments to verify"].count, 1)
        for task in page.context["tasks"]:
            with self.subTest(task=str(task.label)):
                self.assertEqual(self.client.get(task.url).status_code, 200)
        for name in ("video_create", "membership_payments", "members", "withdrawal_requests"):
            self.assertContains(page, f'class="console-quick__item" href="{reverse("administration:" + name)}"')
        self.assertContains(page, "Recent activity")
        self.assertContains(page, self.claim.reference)

    def test_payment_verifier_sees_only_payment_work(self):
        verifier = staff_with("ops-verifier@example.com", "verify_manual_payments")
        self.client.force_login(verifier)
        page = self.client.get(OVERVIEW)
        self.assertEqual([str(s["label"]) for s in page.context["stats"]], ["Confirmed payments"])
        self.assertEqual([str(t.label) for t in page.context["tasks"]], ["Membership payments to verify"])
        self.assertNotContains(page, reverse("administration:members"))

    def test_settings_page_lists_only_permitted_settings(self):
        self.client.force_login(self.root)
        page = self.client.get(reverse("administration:settings"))
        for label in ("Platform settings", "Membership plans", "Payment methods", "Withdrawal settings",
                      "Referral rules", "Video storage", "Staff and permissions", "System health"):
            self.assertContains(page, label)
        manager = make_user("ops-manager@example.com", role=User.Role.CONTENT_MANAGER)
        self.client.force_login(manager)
        page = self.client.get(reverse("administration:settings"))
        self.assertNotContains(page, "Platform settings")
        self.assertNotContains(page, "Withdrawal settings")
        self.assertContains(page, "Video storage")

    def test_system_health_shows_background_job_detail_to_super_admins(self):
        self.client.force_login(self.root)
        page = self.client.get(reverse("administration:health"))
        self.assertContains(page, "The job runner has never checked in on this database.")
        self.assertContains(page, "run_jobs_cron.sh")
        self.assertContains(page, "referrals.evaluate")
        manager = make_user("ops-manager2@example.com", role=User.Role.CONTENT_MANAGER)
        self.client.force_login(manager)
        self.assertNotContains(self.client.get(reverse("administration:health")), "run_jobs_cron.sh")

    def test_navigation_mapping_covers_every_console_page(self):
        rows = navigation.mapping_rows()
        unmapped = [route for route, _url, section, _item, _perm in rows
                    if not section and navigation.ROUTE_TO_ITEM.get(route) is not None]
        self.assertEqual(unmapped, [])
        sections_used = {str(section) for _route, _url, section, _item, _perm in rows if section}
        for expected in ("Overview", "Members", "Membership", "Videos", "Advertising", "Referrals", "Payments",
                         "Rewards and funding", "Wallets", "Withdrawals", "Reports", "Customer support",
                         "System settings", "System administration", "Testing tools"):
            self.assertIn(expected, sections_used)


class HeaderAndMemberPagesTests(AdvertisingTestCase):
    def test_header_shows_name_initials_and_role_never_the_internal_id(self):
        self.member.first_name, self.member.last_name = "Amina", "Juma"
        self.member.save(update_fields=["first_name", "last_name"])
        self.client.force_login(self.member)
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertNotIn(str(self.member.pk), html)
        self.assertNotIn("Account ID", html)
        self.assertIn(">AJ<", html)
        self.assertIn("Amina Juma", html)
        self.assertIn('class="profile-menu__role">Member<', html)
        self.client.force_login(self.root)
        console = self.client.get(OVERVIEW).content.decode()
        self.assertIn('class="profile-menu__role">Administrator<', console)
        self.assertNotIn(f"Account ID", console)

    def test_header_falls_back_safely_without_a_name(self):
        anonymous_name = make_user("noname@example.com")
        self.client.force_login(anonymous_name)
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("My account", html)
        self.assertNotIn("noname@example.com</span>", html)

    @override_settings(SIMULATION_TOOLS_VISIBLE=False, PAYMENTS_MODE="sandbox", DEBUG=True,
                       MANUAL_MEMBERSHIP_PAYMENTS_ENABLED=True)
    def test_normal_mode_hides_demo_and_sandbox_interfaces(self):
        self.active_membership()
        self.client.force_login(self.member)
        html = self.client.get(reverse("dashboard")).content.decode()
        for hidden in ("Demo rewards", "Payments (sandbox)", f'href="{reverse("rewards:history")}"',
                       f'href="{reverse("payments:history")}"', "demo withdrawal", "Sandbox"):
            self.assertNotIn(hidden, html)
        self.assertIn(reverse("payments:manual_history"), html)  # Real membership payments instead.
        self.assertIn('id="card-rewards"', html)  # A real rewards card replaces the demo one.
        wallet = self.client.get(reverse("wallets:wallet")).content.decode()
        self.assertNotIn(f'href="{reverse("rewards:history")}"', wallet)
        # The pages themselves (and their records) are kept for isolated environments.
        self.assertEqual(self.client.get(reverse("rewards:history")).status_code, 200)

    def test_member_payment_history_lists_real_membership_payments(self):
        from apps.memberships import services as memberships

        with MANUAL_ON:
            manual.publish_instruction(self.root, business_number="13579246", recipient_name="CLIPCASH TEST LTD")
            subscription = memberships.choose_plan(self.member, self.plan)
            claim, _ = manual.submit_claim(self.member, subscription, amount=subscription.price_amount,
                                           payer_phone="0781234567", transaction_reference="MPHIST123456")
            self.client.force_login(self.member)
            page = self.client.get(reverse("payments:manual_history"))
        self.assertContains(page, claim.reference)
        self.assertContains(page, "Pending verification")
        other = make_user("other@example.com")
        self.client.force_login(other)
        self.assertNotContains(self.client.get(reverse("payments:manual_history")), claim.reference)
        self.assertTrue(re.search(r"No membership payments yet", self.client.get(
            reverse("payments:manual_history")).content.decode()))
