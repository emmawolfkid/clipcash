import re
from unittest import mock

from django.conf import settings
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.helpers import make_user, run_worker
from apps.administration import summary
from apps.audit.models import AuditEvent
from apps.core.jobs.services import enqueue
from apps.core.models import Job, WorkerHeartbeat
from apps.payments import simulator
from apps.payments.models import PaymentIntent, ReconciliationException, SandboxPosting
from apps.payments.tests.helpers import SANDBOX
from apps.rewards import services as rewards
from apps.rewards.models import DemoReward
from apps.rewards.tests.helpers import RewardsTestCase
from apps.videos.tests.helpers import API_KEY, TOKEN_KEY
from apps.viewing.models import WatchSession
from apps.wallets import wallet_services
from apps.wallets.models import JournalEntry, Wallet
from apps.withdrawals.models import DemoWithdrawal

OVERVIEW = reverse("administration:overview")
PAGES = ("administration:overview", "administration:health", "administration:members", "administration:security")


@SANDBOX
@override_settings(VIEWING_MAX_STARTS_PER_HOUR=100)
class DashboardTestCase(RewardsTestCase):
    def state(self):
        return (AuditEvent.objects.count(), Job.objects.count(), DemoReward.objects.count(),
                WatchSession.objects.count(), Wallet.objects.count(), JournalEntry.objects.count(),
                SandboxPosting.objects.count(), PaymentIntent.objects.count(), DemoWithdrawal.objects.count())


class AccessTests(DashboardTestCase):
    def test_non_staff_are_redirected(self):
        for user in (None, self.member, self.owner):
            if user:
                self.client.force_login(user)
            for name in PAGES:
                with self.subTest(user=getattr(user, "email", "anonymous"), page=name):
                    response = self.client.get(reverse(name))
                    self.assertEqual(response.status_code, 302)
                    self.assertIn(reverse("admin:login"), response["Location"])

    def test_content_manager_sees_only_video_areas(self):
        self.client.force_login(self.manager)
        page = self.client.get(OVERVIEW)
        self.assertEqual(set(page.context["sections"]), {"videos"})
        for hidden in ("Member directory", "Financial simulation", "Security summary", "Demo rewards"):
            self.assertNotContains(page, hidden)
        self.assertContains(page, reverse("administration:videos"))
        for name in ("administration:members", "administration:security"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403)
        member = self.client.get(reverse("administration:member", args=[self.member.pk]))
        self.assertEqual(member.status_code, 403)
        checks = {c.key for c in self.client.get(reverse("administration:health")).context["checks"]}
        self.assertEqual(checks, {"video_provider"})

    def test_super_admin_sees_everything_and_links_work(self):
        self.client.force_login(self.root)
        page = self.client.get(OVERVIEW)
        self.assertEqual(set(page.context["sections"]),
                         {"members", "advertising", "videos", "viewing", "rewards", "payments", "wallets",
                          "withdrawals", "jobs"})
        for group in ("Overview", "Members", "Advertising", "Videos and viewing", "Financial simulation",
                      "Operations", "Security and audit"):
            self.assertContains(page, f">{group}</p>")
        self.assertContains(page, 'aria-current="page" href="' + OVERVIEW + '"')
        links = set(re.findall(r'href="(/(?:manage|admin)/[^"?#]*)"', page.content.decode()))
        self.assertGreater(len(links), 15)
        for href in sorted(links):
            with self.subTest(href=href):
                self.assertEqual(self.client.get(href, follow=True).status_code, 200)

    def test_get_only(self):
        self.client.force_login(self.root)
        for name in PAGES:
            self.assertEqual(self.client.post(reverse(name)).status_code, 405)


class ReadOnlyAndAccuracyTests(DashboardTestCase):
    def test_pages_write_nothing_and_never_evaluate_rewards(self):
        self.approved_policy()
        session = self.verified_session()  # Verified but not evaluated.
        before = self.state()
        self.client.force_login(self.root)
        with mock.patch("apps.videos.providers.bunny._OPENER.open", side_effect=AssertionError("network")):
            for name in PAGES:
                self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
            self.assertEqual(self.client.get(reverse("administration:member", args=[self.member.pk])).status_code,
                             200)
        self.assertEqual(self.state(), before)
        self.assertFalse(DemoReward.objects.filter(session=session).exists())

    def test_counts_and_separated_money(self):
        self.approved_policy(amount=100)
        rewards.evaluate_session(self.verified_session().pk)
        intent = self.membership_intent_for(make_user("payer@x.example"))
        simulator.simulate(self.root, intent, "pay")
        simulator.simulate(self.root, intent, "settle")
        wallet_services.ensure_wallet(self.member)
        _, sections = summary.overview(self.root)
        self.assertEqual(sections["rewards"]["simulated_total"], 100)
        self.assertEqual(sections["payments"]["statuses"]["SETTLED"], 1)
        self.assertEqual((sections["wallets"]["total"], sections["wallets"]["unexpected_balance"]), (1, 0))
        self.assertEqual(sections["viewing"]["outcomes"]["VERIFIED_EVIDENCE"], 1)
        self.assertEqual(sections["advertising"]["funded"], 0)
        self.assertEqual(sections["members"]["current_memberships"], 1)
        page = self.client_for(self.root).get(OVERVIEW)
        self.assertContains(page, "simulated, not money")
        self.assertContains(page, "Not real cash")
        self.assertContains(page, "Every balance must be TZS 0")

    def membership_intent_for(self, member):
        from apps.memberships import services as memberships
        from apps.payments import services as payments
        from apps.payments.tests.helpers import token

        subscription = memberships.choose_plan(member, self.plan)
        return payments.create_membership_intent(member, subscription, idempotency_key=token())

    def client_for(self, user):
        self.client.force_login(user)
        return self.client

    def test_no_secrets_rendered(self):
        self.client.force_login(self.root)
        for name in PAGES:
            content = self.client.get(reverse(name)).content.decode()
            for secret in (API_KEY, TOKEN_KEY, settings.SECRET_KEY, settings.DATABASES["default"].get("PASSWORD")):
                if secret and len(secret) >= 12:  # Short values (e.g. a weak local password) match ordinary words.
                    self.assertNotIn(secret, content, name)

    def test_query_count_is_stable(self):
        self.client.force_login(self.root)

        def count():
            # The test client delivers queued email after every request (DeliveringClient);
            # deliver notices queued by the setup first so only the page is measured.
            run_worker()
            with CaptureQueriesContext(connection) as queries:
                self.client.get(OVERVIEW)
            return len(queries)

        count()
        baseline = count()
        self.approved_policy()
        for index in range(3):
            rewards.evaluate_session(self.verified_session(member=self.new_member(f"q{index}@x.example")).pk)
        self.assertEqual(count(), baseline)


class HealthTests(DashboardTestCase):
    def checks(self):
        return {c.key: c for c in summary.health(self.root)}

    def test_statuses(self):
        checks = self.checks()
        self.assertEqual(checks["jobs"].status, summary.NOT_CHECKED)  # No worker heartbeat yet.
        self.client.force_login(self.root)
        self.assertContains(self.client.get(OVERVIEW), "Not checked")  # Overview agrees with the health page.
        self.assertEqual(checks["email"].status, summary.NOT_CONFIGURED)  # Test (locmem) backend.
        self.assertEqual(checks["video_provider"].status, summary.NOT_CHECKED)  # Configured, never "healthy".
        self.assertEqual(checks["payments"].status, summary.HEALTHY)
        WorkerHeartbeat.objects.create(worker_id="w1", last_seen_at=timezone.now())
        self.assertEqual(self.checks()["jobs"].status, summary.HEALTHY)
        job = enqueue("core.health_check", {})
        Job.objects.filter(pk=job.pk).update(status=Job.Status.FAILED, finished_at=timezone.now())
        self.assertEqual(self.checks()["jobs"].status, summary.ATTENTION)
        ReconciliationException.objects.create(reason="unknown_reference", detail="test")
        self.assertEqual(self.checks()["payments"].status, summary.ATTENTION)

    def test_video_provider_configuration(self):
        with override_settings(VIDEO_PROVIDER="demo"):
            self.assertEqual(self.checks()["video_provider"].status, summary.NOT_CONFIGURED)
        with override_settings(BUNNY_STREAM_API_KEY=""):
            check = self.checks()["video_provider"]
            self.assertEqual(check.status, summary.NOT_CONFIGURED)
            self.assertNotIn("BUNNY_STREAM_API_KEY", check.detail)  # Count only, no setting names or values.

    def test_payments_disabled_is_not_configured(self):
        with override_settings(PAYMENTS_MODE="disabled"):
            self.assertEqual(self.checks()["payments"].status, summary.NOT_CONFIGURED)


class MemberAndSecurityTests(DashboardTestCase):
    def test_member_directory_and_detail(self):
        self.client.force_login(self.root)
        listing = self.client.get(reverse("administration:members"), {"q": "member@"})
        self.assertContains(listing, self.member.email)
        self.assertNotContains(listing, self.root.email)  # Staff are not members.
        wallet = wallet_services.ensure_wallet(self.member)
        wallet_services.place_restriction(self.root, wallet, kind="FRAUD_REVIEW", reason="Secret reviewer note")
        detail = self.client.get(reverse("administration:member", args=[self.member.pk]))
        self.assertContains(detail, wallet.number)
        self.assertContains(detail, "Wallet placed under review")
        self.assertNotContains(detail, "Secret reviewer note")
        self.assertEqual(self.client.get(reverse("administration:member", args=[self.root.pk])).status_code, 404)

    def test_security_page_hides_metadata(self):
        wallet = wallet_services.ensure_wallet(self.member)
        wallet_services.place_restriction(self.root, wallet, kind="FRAUD_REVIEW", reason="Secret reviewer note")
        with self.assertRaises(wallet_services.NotAuthorized):
            wallet_services.place_restriction(self.member, wallet, kind="FRAUD_REVIEW", reason="Not allowed here")
        self.client.force_login(self.root)
        page = self.client.get(reverse("administration:security"))
        self.assertContains(page, "wallets.access.denied")
        self.assertContains(page, "wallets.restriction.placed")
        self.assertNotContains(page, "Secret reviewer note")
        self.assertGreaterEqual(page.context["summary"]["financial_denials"], 1)

    def test_financial_locks_are_still_installed(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
            triggers = {row[0] for row in cursor.fetchall()}
        for name in ("a_wallets_block_member_postings", "wallets_transactions_locked", "withdrawals_request_locked",
                     "advertising_campaign_guard"):
            self.assertIn(name, triggers)
