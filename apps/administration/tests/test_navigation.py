"""ADMIN-01: the administration menu is complete, grouped, permission-aware and highlights one entry.

NAV_MAP is the reviewed mapping between console routes and menu entries. A new console route makes
``test_every_console_route_is_mapped`` fail until someone decides which entry it belongs to.
"""
import re

from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils import translation

from apps.administration.urls import urlpatterns
from apps.advertising.tests.helpers import AdvertisingTestCase, User, make_user
from apps.payments import manual
from apps.payments.tests.manual_helpers import MANUAL_ON, staff_with

# Route name -> menu entry (URL name) highlighted on that page; None: not a page (POST-only action, download).
NAV_MAP = {
    "overview": "overview", "health": "health",
    "members": "members", "member": "members", "member_complimentary": "members",
    "member_membership_action": "members",
    "plans": "plans", "plan_create": "plans", "plan": "plans",
    "payment_instructions": "payment_instructions", "payment_method_new": "payment_instructions",
    "payment_method_edit": "payment_instructions", "payment_method_retire": "payment_instructions",
    "payment_method_reactivate": "payment_instructions",
    "membership_payments": "membership_payments", "membership_payment": "membership_payments",
    "videos": "videos", "video_create": "videos", "video_detail": "videos", "video_upload": None, "video_action": None,
    "categories": "categories", "category_edit": "categories",
    "viewing": "viewing", "viewing_review": "viewing",
    "advertisers": "advertisers", "advertiser_review": "advertisers",
    "campaigns": "campaigns", "campaign_review": "campaigns", "campaign_credits": "demo_rewards",
    # UI-DASH-03: staff-managed advertising.
    "advertiser_create": "advertisers", "campaign_create": "campaign_create", "campaign_edit": "campaigns",
    "demo_rewards": "demo_rewards", "reward_policies": "demo_rewards", "reward_policy": "demo_rewards",
    "payments": "payments", "payment_exceptions": "payments", "payment_funding": "payments", "payment": "payments",
    "wallets": "wallets", "wallet_reconciliation": "wallets", "wallet": "wallets",
    "withdrawals": "withdrawals", "withdrawal_exceptions": "withdrawals", "beneficiaries": "withdrawals",
    "withdrawal": "withdrawals",
    "support": "support", "support_ticket": "support",
    "analytics": "analytics", "reports": "reports", "report": "reports", "report_export": None,
    "security": "security",
}
SECTIONS = ["Overview", "Members and memberships", "Membership payments", "Videos and viewing", "Advertising",
            "Financial simulation", "Customer support", "Reports", "Operations", "Security and audit",
            "Your account"]
LINK = re.compile(r'<a class="app-nav__link( is-current)?"( aria-current="page")? href="([^"]+)">([^<]+)')
GROUP = re.compile(r'<p class="app-nav__group" id="[^"]+">([^<]+)</p>')


def menu(html):
    """(group headings, [(href, label, current)]) of the console menu only."""
    nav = html[html.index('<nav id="console-menu"'):]
    nav = nav[:nav.index("</nav>")]
    links = [(href, label.strip(), bool(cls) and bool(aria)) for cls, aria, href, label in LINK.findall(nav)]
    return GROUP.findall(nav), links


@MANUAL_ON
class AdministrationNavigationTests(AdvertisingTestCase):
    def setUp(self):
        super().setUp()
        from apps.memberships import services as memberships

        self.instruction = manual.publish_instruction(self.root, business_number="13579246",
                                                      recipient_name="CLIPCASH TEST LTD")
        self.campaign = self.running_campaign()
        self.subscription = memberships.choose_plan(self.member, self.plan)
        self.claim, _ = manual.submit_claim(self.member, self.subscription, amount=self.subscription.price_amount,
                                            payer_phone="0781234567", transaction_reference="MPNAV1234567")

    def page(self, url, user=None, status=200):
        self.client.force_login(user or self.root)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status, url)
        return menu(response.content.decode())

    def sample_urls(self):
        """Every console page a Super Administrator can open in this configuration (sandbox payments off)."""
        args = {"member": [self.member.pk], "plan": [self.plan.pk], "video_detail": [self.ad_video.pk],
                "category_edit": [self.category.pk], "advertiser_review": [self.org.pk],
                "campaign_review": [self.campaign.pk], "campaign_credits": [self.campaign.pk],
                "membership_payment": [self.claim.pk], "payment_method_edit": [self.instruction.pk],
                "payment_method_retire": [self.instruction.pk]}
        skip = {"payments", "payment_exceptions", "payment_funding"}  # Sandbox off: refused, tested below.
        names = [n for n, entry in NAV_MAP.items() if entry and n not in skip
                 and (n in args or not any(p.pattern.converters for p in urlpatterns if p.name == n))]
        return {name: reverse(f"administration:{name}", args=args.get(name, [])) for name in names}

    def test_every_console_route_is_mapped(self):
        routes = {p.name for p in urlpatterns}
        self.assertEqual(routes, set(NAV_MAP), "Map each new console route to its menu entry in NAV_MAP.")
        targets = {entry for entry in NAV_MAP.values() if entry}
        self.assertTrue(targets <= routes)

    def test_each_page_highlights_exactly_its_entry(self):
        for name, url in self.sample_urls().items():
            with self.subTest(page=name):
                _groups, links = self.page(url)
                current = [href for href, _label, is_current in links if is_current]
                self.assertEqual(current, [reverse(f"administration:{NAV_MAP[name]}")])

    def test_groups_order_and_no_duplicate_or_broken_links(self):
        groups, links = self.page(reverse("administration:overview"))
        self.assertEqual(groups, SECTIONS)
        hrefs = [href for href, _label, _current in links]
        self.assertEqual(len(hrefs), len(set(hrefs)), hrefs)
        for href in hrefs:
            with self.subTest(href=href):
                # The site settings list redirects to its single record; a login page would mean a dead end.
                response = self.client.get(href, follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("login", response.request["PATH_INFO"])

    def test_super_admin_sees_both_membership_payment_entries_in_order(self):
        _groups, links = self.page(reverse("administration:overview"))
        labels = [label for _href, label, _current in links]
        start = labels.index("Payment methods")
        self.assertEqual(labels[start:start + 2], ["Payment methods", "Verification queue"])
        self.assertIn((reverse("administration:payment_instructions"), "Payment methods", False), links)
        self.assertIn((reverse("administration:membership_payments"), "Verification queue", False), links)

    @override_settings(MANUAL_MEMBERSHIP_PAYMENTS_ENABLED=False)
    def test_switched_off_keeps_methods_reachable_and_hides_the_refused_queue(self):
        queue, methods = reverse("administration:membership_payments"), reverse("administration:payment_instructions")
        self.client.force_login(self.root)
        self.assertEqual(self.client.get(queue).status_code, 403)
        groups, links = self.page(reverse("administration:overview"))
        hrefs = [href for href, _label, _current in links]
        self.assertIn("Membership payments", groups)
        self.assertIn(methods, hrefs)
        self.assertNotIn(queue, hrefs)
        response = self.client.get(methods)
        self.assertContains(response, "Manual membership payments are switched off in this deployment.")
        self.assertNotContains(response, f'href="{queue}"')
        self.assertEqual([h for h, _l, current in menu(response.content.decode())[1] if current], [methods])

    def test_roles_see_only_what_they_may_open(self):
        queue, methods = reverse("administration:membership_payments"), reverse("administration:payment_instructions")
        verifier = staff_with("nav-verifier@example.com", "verify_manual_payments")
        groups, links = self.page(queue, verifier)
        hrefs = [href for href, _label, _current in links]
        self.assertIn(queue, hrefs)
        self.assertNotIn(methods, hrefs)  # Receiving numbers: Super Administrators only.
        self.assertNotIn(reverse("administration:plans"), hrefs)
        self.client.force_login(verifier)
        self.assertEqual(self.client.get(methods).status_code, 403)

        manager = make_user("nav-manager@example.com", role=User.Role.CONTENT_MANAGER)
        groups, links = self.page(reverse("administration:overview"), manager)
        self.assertNotIn("Membership payments", groups)
        for href, _label, _current in links:
            if href.startswith("/manage/"):
                with self.subTest(role="content manager", href=href):
                    self.assertEqual(self.client.get(href).status_code, 200)
        self.assertEqual(self.client.get(methods).status_code, 403)
        self.assertEqual(self.client.get(queue).status_code, 403)

        self.client.force_login(self.member)
        self.assertNotEqual(self.client.get(methods).status_code, 200)
        self.assertNotEqual(self.client.get(reverse("administration:overview")).status_code, 200)

    def test_viewer_permission_alone_shows_the_queue(self):
        viewer = staff_with("nav-viewer@example.com", "view_manual_payments")
        _groups, links = self.page(reverse("administration:membership_payments"), viewer)
        self.assertIn(reverse("administration:membership_payments"), [href for href, _l, _c in links])

    def test_financial_simulation_labels_stay_explicit(self):
        groups, links = self.page(reverse("administration:overview"))
        labels = {label for _href, label, _current in links}
        self.assertIn("Financial simulation", groups)
        self.assertTrue({"Demo rewards", "Wallets (real ledger)", "Withdrawals (demo)"} <= labels)
        self.assertNotIn(reverse("administration:payments"), [h for h, _l, _c in links])  # Sandbox off.
        self.client.force_login(self.root)
        self.assertEqual(self.client.get(reverse("administration:payments")).status_code, 403)

    def test_menu_has_one_mobile_toggle_and_labelled_groups(self):
        self.client.force_login(self.root)
        html = self.client.get(reverse("administration:overview")).content.decode()
        self.assertEqual(html.count('aria-controls="console-menu"'), 1)
        self.assertEqual(html.count('id="console-menu"'), 1)
        for group_id in re.findall(r'<p class="app-nav__group" id="([^"]+)"', html):
            self.assertIn(f'aria-labelledby="{group_id}"', html)
        self.assertNotIn(" style=", html)

    def test_swahili_section_headings_and_wallet_label(self):
        self.addCleanup(translation.activate, translation.get_language())
        self.client.force_login(self.root)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "sw"
        html = self.client.get(reverse("administration:overview")).content.decode()
        groups, links = menu(html)
        self.assertEqual(groups, ["Muhtasari", "Wanachama na uanachama", "Malipo ya uanachama", "Video na utazamaji",
                                  "Matangazo", "Fedha za kuigiza", "Usaidizi kwa wateja", "Ripoti", "Uendeshaji",
                                  "Usalama na ukaguzi", "Akaunti yako"])
        self.assertIn((reverse("administration:wallets"), "Pochi (leja halisi)", False), links)
        self.assertIn('aria-label="Menyu ya usimamizi"', html)
        self.assertIn(">Usimamizi</p>", html)
