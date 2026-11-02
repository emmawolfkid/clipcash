from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.tests.helpers import make_user
from apps.audit.models import AuditEvent
from apps.payments import services as payments
from apps.payments import simulator
from apps.payments.models import PaymentIntent, SandboxPosting
from apps.payments.tests.helpers import SANDBOX, token
from apps.rewards import services as rewards
from apps.rewards.models import DemoReward
from apps.rewards.tests.helpers import RewardsTestCase
from apps.viewing.models import WatchSession
from apps.wallets import wallet_services
from apps.wallets.models import JournalEntry, Wallet
from apps.withdrawals.models import DemoWithdrawal

DASHBOARD = reverse("dashboard")
ACTIVITY = reverse("activity")


@SANDBOX
@override_settings(VIEWING_MAX_STARTS_PER_HOUR=100)
class MemberExperienceTestCase(RewardsTestCase):
    def counts(self):
        return (DemoReward.objects.count(), WatchSession.objects.count(), Wallet.objects.count(),
                JournalEntry.objects.count(), SandboxPosting.objects.count(), PaymentIntent.objects.count(),
                DemoWithdrawal.objects.count())


class AccessTests(MemberExperienceTestCase):
    def test_access_rules(self):
        self.assertEqual(self.client.get(DASHBOARD).status_code, 302)
        self.assertEqual(self.client.get(ACTIVITY).status_code, 302)
        self.client.force_login(make_user("u@x.example", verified=False))
        self.assertRedirects(self.client.get(DASHBOARD), reverse("accounts:verification_required"))
        self.client.force_login(self.manager)  # Staff: page works, no member wallet is implied.
        page = self.client.get(DASHBOARD)
        self.assertContains(page, "Wallets are for verified member accounts")
        self.assertFalse(Wallet.objects.exists())

    def test_navigation_structure(self):
        self.client.force_login(self.member)
        page = self.client.get(DASHBOARD)
        self.assertContains(page, 'aria-current="page" href="' + DASHBOARD + '"')
        for group in ("Home", "Watch", "Money", "Account"):
            self.assertContains(page, f">{group}</p>")
        self.assertContains(page, 'aria-controls="member-menu"')
        self.assertContains(page, "js/member-nav.js")
        self.assertContains(page, 'action="' + reverse("accounts:logout") + '"')
        self.assertNotContains(page, reverse("administration:overview"))
        self.assertNotContains(page, "style=")


class SummaryTests(MemberExperienceTestCase):
    def test_empty_states(self):
        newcomer = make_user("new@x.example")
        self.client.force_login(newcomer)
        page = self.client.get(DASHBOARD)
        for text in ("You do not have an active membership", "0 available", "No demo rewards yet",
                     "Not available", "Withdrawals are not available yet", "No test payments"):
            self.assertContains(page, text)
        self.assertNotContains(page, "TZS 0")  # No wallet yet: no figure at all.
        self.assertFalse(Wallet.objects.filter(member=newcomer).exists())

    def test_full_member_summary(self):
        self.approved_policy(amount=100)
        session = self.verified_session()
        rewards.evaluate_session(session.pk)
        wallet_services.ensure_wallet(self.member)
        self.client.force_login(self.member)
        page = self.client.get(DASHBOARD)
        self.assertContains(page, self.plan.name)
        self.assertContains(page, "Valid until")
        self.assertContains(page, "1 verified view")
        self.assertContains(page, "SIMULATED REWARDS — NOT WITHDRAWABLE.")
        self.assertContains(page, "TZS 100")
        self.assertContains(page, "TZS 0 <span class=\"summary-card__unit\">available (real ledger)</span>", html=False)
        for misleading in ("Available earnings", "Withdraw now", "Guaranteed", "Cash balance"):
            self.assertNotContains(page, misleading)

    def test_reversal_and_isolation(self):
        self.approved_policy(amount=100)
        reward = rewards.evaluate_session(self.verified_session().pk)
        rewards.reverse_reward(self.root, reward, reason="Duplicate account")
        other = self.new_member("other@x.example")
        self.client.force_login(self.member)
        self.assertContains(self.client.get(DASHBOARD), "1 reversed")
        self.client.force_login(other)
        page = self.client.get(DASHBOARD)
        self.assertContains(page, "No demo rewards yet")
        self.assertContains(page, "0 verified views")
        self.assertContains(page, "1 available")  # The campaign is still open to this member.

    def test_sandbox_payments_are_labelled(self):
        from apps.memberships import services as memberships

        newcomer = make_user("pay@x.example")
        subscription = memberships.choose_plan(newcomer, self.plan)
        intent = payments.create_membership_intent(newcomer, subscription, idempotency_key=token())
        simulator.simulate(self.root, intent, "pay")
        simulator.simulate(self.root, intent, "settle")
        self.client.force_login(newcomer)
        page = self.client.get(DASHBOARD)
        self.assertContains(page, "1 test payment")
        self.assertContains(page, "No real money moves")
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, "PENDING")  # Never activated by sandbox money.
        membership_page = self.client.get(reverse("memberships:mine"))
        self.assertContains(membership_page, "never activates your membership")

    def test_dashboard_writes_nothing(self):
        self.approved_policy()
        self.verified_session()  # Verified, not yet evaluated: loading pages must not evaluate it.
        before = self.counts()
        audit_before = AuditEvent.objects.count()
        self.client.force_login(self.member)
        for url in (DASHBOARD, ACTIVITY, reverse("accounts:profile"), reverse("wallets:wallet"),
                    reverse("withdrawals:overview"), reverse("rewards:history"), reverse("viewing:list")):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertEqual(self.counts(), before)
        self.assertEqual(AuditEvent.objects.count(), audit_before)

    def test_dashboard_query_count_is_bounded(self):
        self.client.force_login(self.member)

        def count():
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(DASHBOARD).status_code, 200)
            return len(queries)

        count()
        baseline = count()
        self.approved_policy()
        for index in range(3):
            campaign = self.running_campaign(name=f"Extra {index}")
            self.approved_policy(campaign=campaign)
            rewards.evaluate_session(self.verified_session(campaign=campaign).pk)
        wallet_services.ensure_wallet(self.member)
        grown = count()
        self.assertLessEqual(grown - baseline, 3)  # Fixed overhead (wallet), not per record.
        more = self.running_campaign(name="Extra more")
        self.approved_policy(campaign=more)
        rewards.evaluate_session(self.verified_session(campaign=more).pk)
        self.assertEqual(count(), grown)


class ProfileAndActivityTests(MemberExperienceTestCase):
    def test_profile_security_links(self):
        self.client.force_login(self.member)
        page = self.client.get(reverse("accounts:profile"))
        for url in (reverse("accounts:password_change"), reverse("accounts:password_reset"), reverse("dashboard")):
            self.assertContains(page, f'href="{url}"')
        self.assertContains(page, "Email confirmed")
        self.assertContains(page, "never ask for your password")
        self.assertContains(page, 'aria-label="Breadcrumb"')
        self.assertContains(page, self.plan.name)

    def test_activity_is_private_and_safe(self):
        self.approved_policy(amount=100)
        rewards.evaluate_session(self.verified_session().pk)
        wallet = wallet_services.ensure_wallet(self.member)
        wallet_services.place_restriction(self.root, wallet, kind="FRAUD_REVIEW", reason="Secret reviewer note")
        other = self.new_member("other@x.example")
        self.client.force_login(self.member)
        page = self.client.get(ACTIVITY)
        self.assertContains(page, "Wallet placed under review")
        self.assertContains(page, "View verified")
        self.assertContains(page, "(TZS 100, simulated)")
        self.assertNotContains(page, "Secret reviewer note")
        self.assertNotContains(page, self.root.email)
        self.client.force_login(other)
        page = self.client.get(ACTIVITY)
        self.assertNotContains(page, "View verified")
        self.assertNotContains(page, "Wallet placed under review")

