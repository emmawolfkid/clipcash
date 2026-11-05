"""UI-05: membership, reward, wallet, payment and withdrawal pages say only what the records support."""

import re
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import resolve, reverse
from django.utils import timezone, translation

from apps.accounts.tests.helpers import make_user
from apps.core import features
from apps.memberships import services as memberships
from apps.memberships.models import Subscription
from apps.payments import services as payments
from apps.payments.models import PaymentIntent
from apps.payments.tests.helpers import PaymentsTestCase, token
from apps.rewards import services as rewards
from apps.rewards.tests.helpers import RewardsTestCase
from apps.wallets import wallet_services
from apps.wallets.models import WalletTransaction
from apps.withdrawals import services as withdrawals
from apps.withdrawals.models import DemoWithdrawal
from apps.withdrawals.tests.helpers import WithdrawalsTestCase

CSS = settings.BASE_DIR / "static/css"
TEMPLATES = settings.BASE_DIR / "templates"
FINANCIAL_TEMPLATES = ("memberships/plan_list", "memberships/plan_detail", "memberships/my_membership",
                       "memberships/subscription_detail", "rewards/history", "wallets/wallet",
                       "wallets/transaction", "payments/history", "payments/intent", "payments/funding",
                       "withdrawals/overview", "withdrawals/destinations", "withdrawals/demo")
COMPLIMENTARY = override_settings(RESTRICTED_DEMO_MODE=True, MEMBERSHIPS_COMPLIMENTARY_GRANTS_ENABLED=True)
RAW_CODE = re.compile(r"\bDEMO_[A-Z_]+\b|review_started")


def rule_position(text, selector):
    match = re.search(rf"(^|\n){re.escape(selector)}(?![\w-])[^{{]*{{", text)
    assert match, selector
    return match.start()


class StylesheetIntegrationTests(SimpleTestCase):
    """The browser picks between equal-specificity rules by source order, so the order is the behaviour."""

    def test_notice_variants_follow_the_base_rule(self):
        alerts = (CSS / "components/alerts.css").read_text(encoding="utf-8")
        self.assertLess(rule_position(alerts, ".messages li,"), rule_position(alerts, ".notice--sandbox"))
        self.assertLess(rule_position(alerts, ".messages li,"), rule_position(alerts, ".notice--demo"))

    def test_badge_variants_follow_the_base_rule(self):
        badges = (CSS / "components/badges.css").read_text(encoding="utf-8")
        self.assertLess(rule_position(badges, ".badge"), rule_position(badges, ".badge--sandbox"))
        self.assertLess(rule_position(badges, ".badge"), rule_position(badges, ".badge--draft"))

    def test_every_demo_withdrawal_status_has_a_badge_and_paid_is_never_green(self):
        badges = (CSS / "components/badges.css").read_text(encoding="utf-8")
        for value in DemoWithdrawal.Status.values:
            with self.subTest(status=value):
                self.assertIn(f".badge--{value.lower()}", badges)
        success = badges[rule_position(badges, ".badge--active"):].split("}", 1)[0]
        self.assertNotIn("demo_paid", success)

    def test_table_regions_contain_their_hidden_headings(self):
        tables = (CSS / "components/tables.css").read_text(encoding="utf-8")
        region = tables[rule_position(tables, ".table-scroll"):].split("}", 1)[0]
        self.assertIn("position: relative", region)

    def test_one_shared_large_amount_replaces_the_page_copies(self):
        self.assertFalse((CSS / "pages/payments/intent.css").exists())
        self.assertFalse((CSS / "pages/wallets/transaction.css").exists())
        everything = "\n".join(path.read_text(encoding="utf-8") for path in CSS.rglob("*.css"))
        for retired in (".payment-amount", ".transaction-amount", ".demo-withdrawal-amount"):
            self.assertNotIn(retired, everything)

    def test_templates_have_no_inline_css_and_use_translation_hooks(self):
        for name in FINANCIAL_TEMPLATES:
            with self.subTest(template=name):
                source = (TEMPLATES / f"{name}.html").read_text(encoding="utf-8")
                self.assertNotRegex(source, r"\bstyle=|<style")
                self.assertRegex(source, r"\{% load [^%]*\bi18n\b")
                self.assertRegex(source, r'class="page-title"|advertising/_header')

    def test_financial_tables_have_captions_and_column_scopes(self):
        for name in FINANCIAL_TEMPLATES:
            source = (TEMPLATES / f"{name}.html").read_text(encoding="utf-8")
            for table in re.findall(r"<table.*?</table>", source, flags=re.S):
                with self.subTest(template=name):
                    self.assertIn("<caption>", table)
                    self.assertNotRegex(table, r"<th(?=[\s>])(?![^>]*scope=)")


class MembershipPageTests(PaymentsTestCase):
    def mine(self, member):
        self.client.force_login(member)
        return self.client.get(reverse("memberships:mine"))

    def subscription_page(self, member, subscription):
        self.client.force_login(member)
        return self.client.get(reverse("memberships:subscription_detail", args=[subscription.pk]))

    @COMPLIMENTARY
    def test_complimentary_access_is_not_shown_as_charged(self):
        member = make_user("gift@example.com")
        memberships.grant_complimentary(self.root, member, self.plan, expires_at=timezone.now() + timedelta(days=14),
                                        reason="Demonstration access for review")
        subscription = Subscription.objects.get(member=member)
        self.assertEqual(subscription.amount_paid, 0)
        page = self.mine(member)
        self.assertContains(page, "Complimentary, no payment")
        history = page.content.decode().split("Your membership records", 1)[1]
        self.assertNotIn(f"TZS {self.plan.price_amount:,}", history)
        detail = self.subscription_page(member, subscription)
        self.assertContains(detail, "Listed plan price")
        self.assertContains(detail, "Amount paid")
        self.assertContains(detail, "TZS 0")
        self.assertContains(detail, "no payment was made")

    @COMPLIMENTARY
    def test_current_complimentary_membership_is_labelled_as_unpaid(self):
        member = make_user("gift-current@example.com")
        memberships.grant_complimentary(self.root, member, self.plan, expires_at=timezone.now() + timedelta(days=14),
                                        reason="Demonstration access for review")
        page = self.mine(member)
        self.assertContains(page, "Current plan")
        self.assertContains(page, "Complimentary demonstration access")

    def test_pending_choice_replaces_the_see_plans_prompt_and_keeps_cancel(self):
        member = make_user("choice@example.com")
        subscription = memberships.choose_plan(member, self.plan)
        page = self.mine(member)
        self.assertContains(page, "Your choice below is waiting for payment")
        self.assertNotContains(page, "See plans")
        self.assertContains(page, reverse("memberships:cancel_pending", args=[subscription.pk]))
        self.assertContains(page, "csrfmiddlewaretoken")
        detail = self.subscription_page(member, subscription)
        self.assertContains(detail, "Kept until")
        self.assertContains(detail, "Online payment is not available yet")

    def test_no_membership_empty_state(self):
        page = self.mine(make_user("new@example.com"))
        self.assertContains(page, "You do not have an active membership")
        self.assertContains(page, "See plans")
        self.assertContains(page, "No memberships yet")

    def test_suspended_and_cancelled_records_are_explained(self):
        member = make_user("paused@example.com")
        self.active_membership(member=member)
        subscription = Subscription.objects.get(member=member)
        memberships.suspend(self.root, subscription, "ADMIN_REVIEW")
        self.assertContains(self.subscription_page(member, subscription), "Your membership is suspended")

        other = make_user("cancelled@example.com")
        choice = memberships.choose_plan(other, self.plan)
        memberships.cancel_pending(other, choice)
        self.assertContains(self.subscription_page(other, choice), "This membership choice was cancelled")

    def test_records_stay_private(self):
        member = make_user("owner-of-record@example.com")
        subscription = memberships.choose_plan(member, self.plan)
        self.client.force_login(make_user("someone-else@example.com"))
        response = self.client.get(reverse("memberships:subscription_detail", args=[subscription.pk]))
        self.assertEqual(response.status_code, 404)

    def test_plan_pages_promise_nothing_and_signed_out_visitors_get_a_sign_in_action(self):
        detail = self.client.get(reverse("memberships:plan_detail", args=[self.plan.code]))
        self.assertContains(detail, "not an investment")
        self.assertContains(detail, "Sign in to choose this plan")
        self.assertRegex(detail.content.decode(), r'class="button" href="[^"]*login')
        for misleading in ("Guaranteed", "Discount", "Auto-renew", "Withdrawable"):
            self.assertNotContains(detail, misleading)


@override_settings(VIEWING_MAX_STARTS_PER_HOUR=100)
class RewardPageTests(RewardsTestCase):
    def test_links_each_record_to_its_own_verified_view(self):
        self.approved_policy(amount=70)
        session = self.verified_session()
        rewards.evaluate_session(session.pk)
        self.client.force_login(self.member)
        page = self.client.get(reverse("rewards:history"))
        self.assertContains(page, reverse("viewing:session", args=[session.pk]))
        self.assertContains(page, "Verified view")
        self.assertContains(page, "TZS 70")
        self.assertContains(page, "Simulated rewards only")
        self.client.force_login(self.new_member("other@x.example"))
        self.assertEqual(self.client.get(reverse("viewing:session", args=[session.pk])).status_code, 404)

    def test_reversed_record_stays_visible_and_is_not_counted(self):
        self.approved_policy(amount=70)
        reward = rewards.evaluate_session(self.verified_session().pk)
        rewards.reverse_reward(self.root, reward, reason="Duplicate account")
        self.client.force_login(self.member)
        page = self.client.get(reverse("rewards:history"))
        self.assertContains(page, "Reversed after review")
        self.assertContains(page, "badge--demo_reversed")
        self.assertEqual(list(page.context["totals"])[0]["demo_total"], 0)

    def test_empty_state_points_to_sponsored_videos_and_never_offers_money(self):
        self.client.force_login(self.member)
        page = self.client.get(reverse("rewards:history"))
        self.assertContains(page, "No demo rewards yet")
        self.assertContains(page, reverse("viewing:list"))
        for misleading in ("Withdraw now", "Withdrawable", "Available balance", "Cash out"):
            self.assertNotContains(page, misleading)


class WalletPageTests(WithdrawalsTestCase):
    def test_wallet_offers_no_money_movement_and_stays_at_zero(self):
        wallet = wallet_services.ensure_wallet(self.member)
        self.client.force_login(self.member)
        page = self.client.get(reverse("wallets:wallet"))
        self.assertContains(page, wallet.number)
        self.assertContains(page, "Available real ledger balance")
        self.assertContains(page, "Demo rewards are simulated records and never appear here")
        self.assertEqual(page.content.decode().count('<form method="post" action="/wallet'), 0)
        for misleading in ("Transfer", "Top up", "Withdraw now", "Send money", "Deposit"):
            self.assertNotContains(page, misleading)
        self.assertEqual(WalletTransaction.objects.count(), 0)

    def test_restriction_is_announced_without_its_private_reason(self):
        wallet = wallet_services.ensure_wallet(self.member)
        wallet_services.place_restriction(self.root, wallet, kind="SECURITY_HOLD", reason="Private staff reason")
        self.client.force_login(self.member)
        page = self.client.get(reverse("wallets:wallet"))
        self.assertContains(page, "Your wallet is restricted")
        self.assertContains(page, 'class="notice notice--warning" role="status"')
        self.assertNotContains(page, "Private staff reason")

    def test_transaction_page_shows_reference_date_direction_and_balance(self):
        """The database refuses wallet transactions while money is locked, so this renders an unsaved one."""
        txn = WalletTransaction(reference="WT-EXAMPLE", category=WalletTransaction.Category.REVERSAL,
                                bucket=WalletTransaction.Bucket.PENDING, direction=WalletTransaction.Direction.OUT,
                                amount=Decimal("1500"), created_at=timezone.now())
        request = RequestFactory().get(reverse("wallets:wallet"))
        request.user = self.member
        request.resolver_match = resolve(reverse("wallets:wallet"))
        html = render_to_string("wallets/transaction.html", {"txn": txn, "nav": "wallet"}, request=request)
        for text in ("WT-EXAMPLE", "Reversal", "Pending", "Out", "TZS 1,500", "Wallet money is not active yet"):
            self.assertIn(text, html)

    def test_financial_features_stay_off_by_default(self):
        with override_settings(PAYMENTS_MODE="disabled", PAYMENTS_SANDBOX_ALLOWED=False):
            self.assertEqual([key for key in features.FINANCIAL_FEATURES if features.is_enabled(key)], [])


class PaymentPageTests(PaymentsTestCase):
    def test_every_intent_status_has_a_badge_and_honest_copy(self):
        badges = (CSS / "components/badges.css").read_text(encoding="utf-8")
        source = (TEMPLATES / "payments/intent.html").read_text(encoding="utf-8")
        for value in PaymentIntent.Status.values:
            with self.subTest(status=value):
                self.assertIn(f".badge--{value.lower()}", badges)
                self.assertIn(f'intent.status == "{value}"', source)

    def test_intent_page_keeps_cancel_form_and_says_nothing_is_charged(self):
        intent = self.membership_intent()
        self.client.force_login(self.member)
        page = self.client.get(reverse("payments:intent", args=[intent.pk]))
        self.assertContains(page, "Nothing is charged")
        self.assertContains(page, "SANDBOX: simulated payments")
        self.assertContains(page, "csrfmiddlewaretoken")
        payments.cancel_intent(self.member, intent)
        ended = self.client.get(reverse("payments:intent", args=[intent.pk]))
        self.assertContains(ended, "This test payment has ended")
        self.assertNotContains(ended, "Cancel payment")

    def test_funding_uses_formatted_amounts_captions_and_stays_in_sandbox_terms(self):
        payments.create_deposit_intent(self.owner, self.org, amount=1_500_000, idempotency_key=token())
        self.client.force_login(self.owner)
        page = self.client.get(reverse("payments:funding", args=[self.org.pk]))
        self.assertContains(page, "TZS 1,500,000")
        self.assertContains(page, "Sandbox deposit requests")
        self.assertContains(page, "Settled deposits (sandbox)")
        self.assertContains(page, 'name="form_token"')
        self.assertContains(page, "SANDBOX: simulated payments")
        self.assertNotRegex(page.content.decode(), r"TZS \d{4,}")

    def test_funding_stays_scoped_to_the_organization(self):
        self.client.force_login(make_user("outsider@example.com"))
        self.assertEqual(self.client.get(reverse("payments:funding", args=[self.org.pk])).status_code, 404)

    def test_payment_pages_disappear_when_payments_are_disabled(self):
        intent = self.membership_intent()
        self.client.force_login(self.member)
        with override_settings(PAYMENTS_MODE="disabled", PAYMENTS_SANDBOX_ALLOWED=False):
            self.assertEqual(self.client.get(reverse("payments:history")).status_code, 404)
            self.assertEqual(self.client.get(reverse("payments:intent", args=[intent.pk])).status_code, 404)
            mine = self.client.get(reverse("memberships:mine"))
            self.assertContains(mine, "Online payment is not available yet")
            self.assertNotContains(mine, reverse("payments:history"))


class WithdrawalPageTests(WithdrawalsTestCase):
    def test_demo_history_shows_labels_not_stored_codes(self):
        demo = self.demo(50_000)
        demo = withdrawals.start_review(self.approver, demo, expected_status=demo.status)
        self.client.force_login(self.member)
        page = self.client.get(reverse("withdrawals:demo", args=[demo.pk]))
        html = page.content.decode()
        self.assertContains(page, "Demo: requested")
        self.assertContains(page, "Demo: under review")
        self.assertContains(page, "badge--demo_under_review")
        self.assertContains(page, "TZS 50,000")
        self.assertContains(page, "DEMO / SIMULATED")
        self.assertContains(page, "•••• 5678")
        visible = re.sub(r"<[^>]+>", " ", html)
        self.assertIsNone(RAW_CODE.search(visible), RAW_CODE.search(visible))
        self.assertNotIn("712345678", html)
        self.assertNotIn("Asha Juma", html)

    def test_preview_formats_the_amount_and_stores_nothing(self):
        self.client.force_login(self.member)
        page = self.client.get(reverse("withdrawals:overview"), {"preview_amount": "600000"})
        self.assertContains(page, "For TZS 600,000")
        self.assertContains(page, '<label for="pv">')
        self.assertFalse(DemoWithdrawal.objects.exists())

    def test_overview_says_withdrawals_are_unavailable_and_offers_no_request_form(self):
        self.client.force_login(self.member)
        page = self.client.get(reverse("withdrawals:overview"))
        self.assertContains(page, "Withdrawals are not available yet")
        self.assertNotIn('action="/withdrawals/request', page.content.decode())
        self.assertNotContains(page, "Withdraw now")

    def test_destination_forms_keep_their_contract_and_masking(self):
        self.mobile(number="0687 123 456")
        self.client.force_login(self.member)
        page = self.client.get(reverse("withdrawals:destinations"))
        self.assertContains(page, "•••• 3456")
        self.assertNotContains(page, "687123456")
        self.assertContains(page, 'aria-describedby="m-number-help"')
        self.assertContains(page, 'aria-describedby="b-number-help"')
        self.assertContains(page, "Verification")
        for field in ('name="action" value="add"', 'name="kind" value="MOBILE_MONEY"', 'name="kind" value="BANK"',
                      'name="identifier"', 'name="holder_name"', 'name="network"', 'name="bank_code"',
                      'name="action" value="remove"', "csrfmiddlewaretoken"):
            self.assertContains(page, field)

    def test_demo_detail_stays_private(self):
        demo = self.demo()
        self.client.force_login(make_user("not-the-owner@example.com"))
        self.assertEqual(self.client.get(reverse("withdrawals:demo", args=[demo.pk])).status_code, 404)


class SwahiliRenderingTests(WithdrawalsTestCase):
    """Pages must still render under the Swahili locale. Wording has no catalogue yet (see the UI-05 doc)."""

    def test_financial_pages_render_with_the_swahili_locale_active(self):
        wallet_services.ensure_wallet(self.member)
        demo = self.demo()
        self.client.force_login(self.member)
        with translation.override("sw"):
            for path in (reverse("memberships:plans"), reverse("memberships:mine"), reverse("wallets:wallet"),
                         reverse("withdrawals:overview"), reverse("withdrawals:destinations"),
                         reverse("withdrawals:demo", args=[demo.pk]), reverse("payments:history")):
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path, headers={"accept-language": "sw"}).status_code, 200)
