import uuid
from unittest import skipUnless

from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from apps.accounts.tests.helpers import make_user
from apps.audit.models import AuditEvent
from apps.memberships.tests.test_concurrency import run_threads
from apps.payments import simulator
from apps.payments.tests.helpers import PaymentsTestCase
from apps.rewards import services as rewards
from apps.rewards.tests.helpers import RewardsTestCase
from apps.wallets import wallet_services
from apps.wallets.models import LedgerAccount, Wallet

from .helpers import User


class MemberWalletPageTests(TestCase):
    def setUp(self):
        self.member = make_user("member@example.com")

    def test_open_and_view_wallet(self):
        self.client.force_login(self.member)
        url = reverse("wallets:wallet")
        page = self.client.get(url)
        self.assertContains(page, "Open my wallet")
        self.assertFalse(Wallet.objects.exists())  # GET never creates anything.
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.member)
        self.assertEqual(csrf.post(url).status_code, 403)
        self.client.post(url)
        page = self.client.get(url)
        self.assertContains(page, "TZS 0")
        self.assertContains(page, "Demo rewards are simulated records and never appear here")
        self.assertContains(page, "No wallet transactions")
        self.assertContains(page, Wallet.objects.get().number)

    def test_staff_and_privacy(self):
        staff = make_user("cm@example.com", role=User.Role.CONTENT_MANAGER)
        self.client.force_login(staff)
        self.assertContains(self.client.get(reverse("wallets:wallet")), "Staff accounts do not have member wallets")
        self.client.post(reverse("wallets:wallet"))
        self.assertFalse(Wallet.objects.exists())
        self.client.force_login(self.member)
        missing = reverse("wallets:transaction", args=[uuid.uuid4()])
        self.assertEqual(self.client.get(missing).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("wallets:wallet")).status_code, 302)


class ConsoleTests(TestCase):
    def setUp(self):
        self.member = make_user("member@example.com")
        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.second = make_user("second@example.com", role=User.Role.SUPER_ADMIN)
        self.wallet = wallet_services.ensure_wallet(self.member)

    def test_console_access_search_and_restriction(self):
        self.client.force_login(make_user("cm@example.com", role=User.Role.CONTENT_MANAGER))
        self.assertEqual(self.client.get(reverse("administration:wallets")).status_code, 403)
        self.assertEqual(self.client.get(reverse("administration:wallet", args=[self.wallet.pk])).status_code, 403)
        self.client.force_login(self.root)
        listing = self.client.get(reverse("administration:wallets"), {"q": "member@"})  # Partial: no match.
        self.assertNotContains(listing, self.wallet.number)
        listing = self.client.get(reverse("administration:wallets"), {"q": self.wallet.number})
        self.assertContains(listing, self.wallet.number)
        detail_url = reverse("administration:wallet", args=[self.wallet.pk])
        detail = self.client.get(detail_url)
        self.assertContains(detail, "There are no balance controls")
        for forbidden in ("Adjust balance", "Credit", "Debit"):
            self.assertNotContains(detail, f">{forbidden}<")
        self.assertTrue(AuditEvent.objects.filter(action="wallets.wallet.inspected").exists())
        self.client.post(detail_url, {"action": "restrict", "kind": "SECURITY_HOLD", "reason": "Account takeover"})
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.status, "RESTRICTED")
        restriction = self.wallet.restrictions.get()
        self.client.post(detail_url, {"action": "request_lift", "restriction": restriction.pk, "reason": "Recovered"})
        self.client.post(detail_url, {"action": "decide_lift", "restriction": restriction.pk, "approve": "1"})
        restriction.refresh_from_db()
        self.assertEqual(restriction.status, "LIFT_REQUESTED")  # Same person cannot approve.
        self.client.force_login(self.second)
        self.client.post(detail_url, {"action": "decide_lift", "restriction": restriction.pk, "approve": "1"})
        restriction.refresh_from_db()
        self.assertEqual(restriction.status, "LIFTED")
        self.assertEqual(self.client.get(reverse("administration:wallet", args=[uuid.uuid4()])).status_code, 404)

    def test_reconciliation_page(self):
        self.client.force_login(self.root)
        url = reverse("administration:wallet_reconciliation")
        self.assertContains(self.client.get(url), "No findings")
        response = self.client.post(url, {"action": "run"}, follow=True)
        self.assertContains(response, "Checked 1 wallets: 0 findings")


@override_settings(VIEWING_MAX_STARTS_PER_HOUR=100)
class DemoRewardsNeverCreditedTests(RewardsTestCase):
    def test_qualified_demo_reward_leaves_wallet_at_zero(self):
        wallet = wallet_services.ensure_wallet(self.member)
        self.approved_policy(amount=100)
        reward = rewards.evaluate_session(self.verified_session().pk)
        self.assertEqual(reward.status, "DEMO_QUALIFIED")
        balances = wallet_services.balances(wallet)
        self.assertEqual((balances.available, balances.pending, balances.reserved), (0, 0, 0))
        self.assertEqual(wallet_services.reconcile()["findings"], 0)


class SandboxPaymentsNeverCreditedTests(PaymentsTestCase):
    def test_settled_sandbox_payments_leave_wallet_at_zero(self):
        wallet = wallet_services.ensure_wallet(self.member)
        intent = self.membership_intent()
        simulator.simulate(self.root, intent, "pay")
        simulator.simulate(self.root, intent, "settle")
        self.settled_deposit(50_000)
        self.assertEqual(wallet_services.balances(wallet).available, 0)
        self.assertEqual(wallet_services.reconcile()["findings"], 0)


@skipUnless(connection.vendor == "postgresql", "Concurrency is verified on PostgreSQL.")
class WalletConcurrencyTests(TransactionTestCase):
    def test_parallel_wallet_creation_creates_one(self):
        member = make_user("member@example.com")
        results = run_threads(5, lambda i: wallet_services.ensure_wallet(member))
        self.assertTrue(all(isinstance(r, Wallet) for r in results), results)
        self.assertEqual(len({r.pk for r in results}), 1)
        self.assertEqual(Wallet.objects.count(), 1)
        self.assertEqual(LedgerAccount.objects.filter(scope_id=str(member.pk)).count(), 3)
