from django.test import Client, override_settings
from django.urls import reverse

from apps.rewards import services

from .helpers import DemoReward, RewardPolicy, RewardsTestCase


@override_settings(VIEWING_MAX_STARTS_PER_HOUR=100)
class MemberPageTests(RewardsTestCase):
    def test_history_is_private_and_labelled(self):
        self.approved_policy(amount=70)
        services.evaluate_session(self.verified_session().pk)
        other = self.new_member("other@x.example")
        self.client.force_login(self.member)
        page = self.client.get(reverse("rewards:history"))
        self.assertContains(page, "Simulated rewards only")
        self.assertContains(page, "TZS 70")
        self.assertContains(page, self.campaign.name)
        for misleading in ("Available balance", "Withdrawable", "Payout pending", "Guaranteed"):
            self.assertNotContains(page, misleading)
        self.client.force_login(other)
        self.assertNotContains(self.client.get(reverse("rewards:history")), "TZS 70")

    def test_reversal_is_visible(self):
        self.approved_policy()
        reward = services.evaluate_session(self.verified_session().pk)
        services.reverse_reward(self.root, reward, reason="Duplicate account")
        self.client.force_login(self.member)
        self.assertContains(self.client.get(reverse("rewards:history")), "Reversed after review")

    def test_requires_verified_account(self):
        self.assertEqual(self.client.get(reverse("rewards:history")).status_code, 302)


class ConsoleTests(RewardsTestCase):
    def test_permissions(self):
        for user in (self.manager, self.member):
            self.client.force_login(user)
            for name in ("demo_rewards", "reward_policies"):
                with self.subTest(user=user.email, page=name):
                    self.assertIn(self.client.get(reverse(f"administration:{name}")).status_code, (302, 403))
        self.client.force_login(self.manager)
        self.client.post(reverse("administration:reward_policies"), {"campaign": self.campaign.pk,
                                                                       "reward_amount": "50"})
        self.assertFalse(RewardPolicy.objects.exists())

    def test_policy_flow_with_four_eyes(self):
        self.client.force_login(self.root)
        self.client.post(reverse("administration:reward_policies"), {"campaign": self.campaign.pk,
                                                                       "reward_amount": "50", "notes": "Launch"})
        policy = RewardPolicy.objects.get()
        url = reverse("administration:reward_policy", args=[policy.pk])
        self.client.post(url, {"action": "approve", "expected_status": "DRAFT"})
        policy.refresh_from_db()
        self.assertEqual(policy.status, "DRAFT")  # The author cannot approve.
        self.client.force_login(self.approver)
        self.client.post(url, {"action": "approve", "expected_status": "DRAFT"})
        policy.refresh_from_db()
        self.assertEqual(policy.status, "APPROVED")
        bad = self.client.post(reverse("administration:reward_policies"),
                               {"campaign": self.campaign.pk, "reward_amount": "12.5"}, follow=True)
        self.assertContains(bad, "whole number")
        self.assertEqual(self.client.get(url).status_code, 200)

    @override_settings(VIEWING_MAX_STARTS_PER_HOUR=100)
    def test_rewards_console_evaluate_and_reverse(self):
        self.approved_policy()
        self.verified_session()
        self.client.force_login(self.root)
        listing = self.client.get(reverse("administration:demo_rewards"))
        self.assertContains(listing, "1 verified view waiting")
        self.client.post(reverse("administration:demo_rewards"), {"action": "evaluate"})
        reward = DemoReward.objects.get()
        self.assertEqual(reward.status, "DEMO_QUALIFIED")
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.root)
        self.assertEqual(csrf.post(reverse("administration:demo_rewards"),
                                   {"action": "reverse", "reward": reward.pk, "reason": "x" * 10}).status_code, 403)
        self.client.post(reverse("administration:demo_rewards"),
                         {"action": "reverse", "reward": reward.pk, "reason": "Duplicate account"})
        reward.refresh_from_db()
        self.assertEqual(reward.status, "DEMO_REVERSED")
        self.assertContains(self.client.get(reverse("administration:demo_rewards"), {"status": "DEMO_REVERSED"}),
                            "Duplicate account")

    @override_settings(VIEWING_MAX_STARTS_PER_HOUR=100)
    def test_revoke_verified_view_from_viewing_console(self):
        self.approved_policy()
        session = self.verified_session()
        services.evaluate_session(session.pk)
        self.client.force_login(self.root)
        url = reverse("administration:viewing_review", args=[session.pk])
        self.assertContains(self.client.get(url), "Revoke verified view")
        self.client.post(url, {"decision": "revoke", "reason": "Fraud confirmed"})
        self.assertEqual(DemoReward.objects.get().status, "DEMO_REVERSED")
        self.assertEqual(services.budget_summary(self.campaign)["demo_consumed"], 0)
