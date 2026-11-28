"""ADMIN-01 / OPS-01: the Operations Console menu is complete, grouped, permission-aware, searchable and
highlights one entry; the console opens in Swahili until a language is chosen.

``navigation.ROUTE_TO_ITEM`` is the reviewed mapping between console routes and menu entries. A new
console route makes ``test_every_console_route_is_mapped`` fail until someone decides where it belongs.
"""
import re

from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils import translation

from apps.administration import navigation
from apps.administration.urls import urlpatterns
from apps.advertising.tests.helpers import AdvertisingTestCase, User, make_user
from apps.payments import manual
from apps.payments.tests.manual_helpers import MANUAL_ON, staff_with

GROUPS = ["Overview", "Management", "Finance", "Services and reports", "System", "Testing tools", "Your account"]
LINK = re.compile(r'<a class="([^"]*)"((?: [a-z-]+="[^"]*")*)>(.*?)</a>', re.S)
GROUP = re.compile(r'<p class="app-nav__group console-nav__group" id="[^"]+">([^<]+)</p>')
TAG = re.compile(r"<[^>]+>")


def menu(html):
    """(group headings, [(href, label, current)]) of the console menu, excluding the collapsed-rail
    duplicates (aria-hidden section links)."""
    nav = html[html.index('<nav id="console-menu"'):]
    nav = nav[:nav.index("</nav>")]
    links = []
    for classes, attributes, inner in LINK.findall(nav):
        if 'aria-hidden="true"' in attributes:
            continue
        href = re.search(r'href="([^"]+)"', attributes).group(1)
        label = " ".join(TAG.sub(" ", inner).split())
        links.append((href, label, 'aria-current="page"' in attributes))
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
                "video_reward": [self.campaign.pk],
                "membership_payment": [self.claim.pk], "payment_method_edit": [self.instruction.pk],
                "payment_method_retire": [self.instruction.pk]}
        skip = {"payments", "payment_exceptions", "payment_funding"}  # Sandbox off: refused, tested below.
        names = [n for n, entry in navigation.ROUTE_TO_ITEM.items() if entry and n not in skip
                 and (n in args or not any(p.pattern.converters for p in urlpatterns if p.name == n))]
        return {name: reverse(f"administration:{name}", args=args.get(name, [])) for name in names}

    def test_every_console_route_is_mapped(self):
        routes = {p.name for p in urlpatterns}
        self.assertEqual(routes, set(navigation.ROUTE_TO_ITEM), "Place each new console route in ROUTE_TO_ITEM.")
        targets = {entry for entry in navigation.ROUTE_TO_ITEM.values() if entry}
        self.assertTrue(targets <= set(navigation.ITEMS))
        # Every console route is a page of a section, or an action/download of a mapped page.
        rows = navigation.mapping_rows()
        self.assertEqual(len(rows), len(urlpatterns))

    def test_each_page_highlights_exactly_its_entry(self):
        for name, url in self.sample_urls().items():
            with self.subTest(page=name):
                _groups, links = self.page(url)
                current = [href for href, _label, is_current in links if is_current]
                item = navigation.ITEMS[navigation.ROUTE_TO_ITEM[name]]
                self.assertEqual(current, [reverse(item.target)])

    def test_current_section_is_open_and_others_are_closed(self):
        self.client.force_login(self.root)
        html = self.client.get(reverse("administration:withdrawal_settings")).content.decode()
        sections = re.findall(r'<details class="console-nav__details"( open)?>\s*<summary[^>]*>.*?'
                              r'<span class="app-nav__label">([^<]+)</span>', html, re.S)
        opened = [label for is_open, label in sections if is_open]
        self.assertEqual(opened, ["Withdrawals"])

    def test_groups_order_and_no_duplicate_or_broken_links(self):
        groups, links = self.page(reverse("administration:overview"))
        self.assertEqual(groups, GROUPS)
        hrefs = [href for href, _label, _current in links]
        self.assertEqual(len(hrefs), len(set(hrefs)), hrefs)
        for href in hrefs:
            with self.subTest(href=href):
                # The site settings list redirects to its single record; a login page would mean a dead end.
                response = self.client.get(href, follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("login", response.request["PATH_INFO"])

    def test_every_menu_entry_has_both_language_names_for_search(self):
        self.client.force_login(self.root)
        html = self.client.get(reverse("administration:overview")).content.decode()
        search = dict(re.findall(r'href="([^"]+)" data-search="([^"]+)"', html))
        self.assertIn("verify payments hakiki malipo", search[reverse("administration:membership_payments")])
        self.assertIn("withdrawal requests maombi ya kutoa pesa", search[reverse("administration:withdrawal_requests")])
        self.assertIn('data-console-search', html)

    def test_payment_entries_in_order(self):
        _groups, links = self.page(reverse("administration:overview"))
        labels = [label for _href, label, _current in links]
        start = labels.index("Verify payments")
        self.assertEqual(labels[start:start + 2], ["Verify payments", "Payment methods"])
        self.assertIn((reverse("administration:payment_instructions"), "Payment methods", False), links)
        self.assertIn((reverse("administration:membership_payments"), "Verify payments", False), links)

    @override_settings(MANUAL_MEMBERSHIP_PAYMENTS_ENABLED=False)
    def test_switched_off_keeps_methods_reachable_and_hides_the_refused_queue(self):
        queue, methods = reverse("administration:membership_payments"), reverse("administration:payment_instructions")
        self.client.force_login(self.root)
        self.assertEqual(self.client.get(queue).status_code, 403)
        groups, links = self.page(reverse("administration:overview"))
        hrefs = [href for href, _label, _current in links]
        self.assertIn("Finance", groups)
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
        self.assertNotIn("Finance", groups)
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

    def test_testing_tools_are_listed_only_when_switched_on(self):
        groups, links = self.page(reverse("administration:overview"))
        labels = {label for _href, label, _current in links}
        self.assertIn("Testing tools", groups)
        self.assertTrue({"Demo rewards", "Withdrawals (demo)"} <= labels)
        self.assertNotIn(reverse("administration:payments"), [h for h, _l, _c in links])  # Sandbox off.
        with override_settings(SIMULATION_TOOLS_VISIBLE=False):
            groups, links = self.page(reverse("administration:overview"))
            self.assertNotIn("Testing tools", groups)
            hrefs = [h for h, _l, _c in links]
            for name in ("demo_rewards", "withdrawals", "withdrawal_exceptions"):
                self.assertNotIn(reverse(f"administration:{name}"), hrefs)
            # The pages and their records stay reachable directly.
            self.assertEqual(self.client.get(reverse("administration:demo_rewards")).status_code, 200)
        self.client.force_login(self.root)
        self.assertEqual(self.client.get(reverse("administration:payments")).status_code, 403)

    def test_menu_has_one_toggle_one_menu_and_labelled_groups(self):
        self.client.force_login(self.root)
        html = self.client.get(reverse("administration:overview")).content.decode()
        self.assertEqual(html.count('aria-controls="console-menu"'), 2)  # Menu button and the search box.
        self.assertEqual(html.count('id="console-menu"'), 1)
        self.assertIn("data-collapsible", html)
        for group_id in re.findall(r'<p class="app-nav__group console-nav__group" id="([^"]+)"', html):
            self.assertIn(f'aria-labelledby="{group_id}"', html)
        self.assertNotIn(" style=", html)

    def test_swahili_section_headings_and_labels(self):
        self.addCleanup(translation.activate, translation.get_language())
        self.client.force_login(self.root)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "sw"
        html = self.client.get(reverse("administration:overview")).content.decode()
        groups, links = menu(html)
        self.assertEqual(groups, ["Muhtasari", "Usimamizi", "Fedha", "Huduma na ripoti", "Mfumo",
                                  "Zana za majaribio", "Akaunti yako"])
        labels = {label for _href, label, _current in links}
        for label in ("Wanachama", "Hakiki malipo", "Maombi ya kutoa pesa", "Pakia video", "Mialiko", "Mipangilio yote"):
            self.assertIn(label, labels)

    @override_settings(CONSOLE_DEFAULT_LANGUAGE="sw")
    def test_console_defaults_to_swahili_until_a_language_is_chosen(self):
        self.addCleanup(translation.activate, translation.get_language())
        self.client.force_login(self.root)
        html = self.client.get(reverse("administration:overview"), HTTP_ACCEPT_LANGUAGE="en").content.decode()
        self.assertIn('<html lang="sw">', html)
        self.assertIn(">Muhtasari</p>", html)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        html = self.client.get(reverse("administration:overview")).content.decode()
        self.assertIn('<html lang="en">', html)
        # Member pages keep the normal negotiation.
        self.client.cookies.pop(settings.LANGUAGE_COOKIE_NAME)
        self.client.force_login(self.member)
        member = self.client.get(reverse("dashboard"), HTTP_ACCEPT_LANGUAGE="en").content.decode()
        self.assertIn('<html lang="en">', member)
