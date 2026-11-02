import json
import uuid
from unittest import skipUnless

from django.db import connection
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse

from apps.accounts.tests.helpers import make_user
from apps.memberships.tests.test_concurrency import run_threads
from apps.payments.tests.helpers import SANDBOX
from apps.withdrawals import services
from apps.withdrawals.models import DemoWithdrawal, PayoutBeneficiary
from apps.withdrawals.payouts import SandboxPayoutProvider

from .helpers import User, WithdrawalsTestCase, token


class MemberPageTests(WithdrawalsTestCase):
    def test_overview_explains_and_previews(self):
        self.client.force_login(self.member)
        page = self.client.get(reverse("withdrawals:overview"))
        self.assertContains(page, "Withdrawals are not available yet")
        self.assertContains(page, "TZS 0")
        self.assertContains(page, "never ask for your mobile-money PIN")
        preview = self.client.get(reverse("withdrawals:overview"), {"preview_amount": "600000"})
        self.assertContains(preview, "would need two ClipCash reviewers")
        self.assertFalse(DemoWithdrawal.objects.exists())  # A preview stores nothing.

    def test_destinations_are_masked_and_private(self):
        self.client.force_login(self.member)
        url = reverse("withdrawals:destinations")
        self.client.post(url, {"action": "add", "kind": "MOBILE_MONEY", "network": "AIRTEL",
                               "identifier": "0687 123 456", "holder_name": "Asha Juma"})
        page = self.client.get(url)
        self.assertContains(page, "•••• 3456")
        self.assertNotContains(page, "687123456")
        beneficiary = PayoutBeneficiary.objects.get()
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.member)
        self.assertEqual(csrf.post(url, {"action": "remove", "beneficiary": beneficiary.pk}).status_code, 403)
        self.client.force_login(make_user("other@x.example"))
        self.assertEqual(self.client.post(url, {"action": "remove", "beneficiary": beneficiary.pk}).status_code, 404)
        beneficiary.refresh_from_db()
        self.assertEqual(beneficiary.status, "ACTIVE")

    def test_demo_detail_is_private(self):
        demo = self.demo()
        self.client.force_login(self.member)
        page = self.client.get(reverse("withdrawals:demo", args=[demo.pk]))
        self.assertContains(page, "DEMO / SIMULATED")
        self.client.force_login(make_user("other@x.example"))
        self.assertEqual(self.client.get(reverse("withdrawals:demo", args=[demo.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("withdrawals:demo", args=[uuid.uuid4()])).status_code, 404)


class ConsoleTests(WithdrawalsTestCase):
    def test_permissions_and_flow(self):
        self.mobile()
        self.client.force_login(self.manager)
        for name in ("withdrawals", "withdrawal_exceptions", "beneficiaries"):
            self.assertEqual(self.client.get(reverse(f"administration:{name}")).status_code, 403)
        self.client.force_login(self.root)
        lookup = self.client.get(reverse("administration:withdrawals"), {"member": self.member.email})
        self.assertContains(lookup, "•••• 5678")
        beneficiary = PayoutBeneficiary.objects.get()
        self.client.post(reverse("administration:withdrawals"), {
            "member": self.member.pk, "beneficiary": beneficiary.pk, "amount": "40000", "form_token": token()})
        demo = DemoWithdrawal.objects.get()
        url = reverse("administration:withdrawal", args=[demo.pk])
        self.client.post(url, {"action": "start_review", "expected_status": "DEMO_REQUESTED"})
        self.client.post(url, {"action": "approve", "expected_status": "DEMO_UNDER_REVIEW"})
        demo.refresh_from_db()
        self.assertIsNone(demo.approved_by)  # Creator cannot approve.
        for reviewer in (self.approver, self.third):
            self.client.force_login(reviewer)
            self.client.post(url, {"action": "approve", "expected_status": "DEMO_UNDER_REVIEW"})
        demo.refresh_from_db()
        self.assertEqual(demo.status, "DEMO_APPROVED")
        self.client.force_login(self.root)
        self.client.post(url, {"action": "initiate", "expected_status": "DEMO_APPROVED"})
        self.client.post(url, {"action": "simulate", "simulate": "succeed"})
        demo.refresh_from_db()
        self.assertEqual(demo.status, "DEMO_PAID")
        page = self.client.get(url)
        self.assertContains(page, "DEMO / SIMULATED")
        self.assertNotContains(page, "712345678")
        self.assertEqual(self.client.get(reverse("administration:beneficiaries"), {"q": self.member.email})
                         .status_code, 200)

    def test_callback_endpoint(self):
        demo = services.initiate_payout(self.root, self.approved())
        from apps.withdrawals.models import SandboxPayout

        SandboxPayout.objects.filter(reference=demo.payout_reference).update(status="SUCCEEDED", sequence=1)
        body = json.dumps({"event_id": f"{demo.payout_reference}:1", "reference": demo.payout_reference},
                          separators=(",", ":")).encode()
        url = reverse("withdrawals:sandbox_callback")
        provider = Client(enforce_csrf_checks=True)
        self.assertEqual(provider.post(url, body, content_type="application/json",
                                       HTTP_X_SANDBOX_SIGNATURE="0" * 64).status_code, 401)
        ok = provider.post(url, body, content_type="application/json",
                           HTTP_X_SANDBOX_SIGNATURE=SandboxPayoutProvider.sign(body))
        self.assertEqual(ok.json()["outcome"], "ACCEPTED")
        replay = provider.post(url, body, content_type="application/json",
                               HTTP_X_SANDBOX_SIGNATURE=SandboxPayoutProvider.sign(body))
        self.assertEqual(replay.json()["outcome"], "DUPLICATE")
        with override_settings(DEBUG=False, PAYMENTS_SANDBOX_ALLOWED=False):
            self.assertEqual(provider.post(url, body, content_type="application/json",
                                           HTTP_X_SANDBOX_SIGNATURE="x").status_code, 404)


@SANDBOX
@skipUnless(connection.vendor == "postgresql", "Concurrency is verified on PostgreSQL.")
class WithdrawalConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.approver = make_user("approver@example.com", role=User.Role.SUPER_ADMIN)
        self.member = make_user("member@example.com")
        self.beneficiary = services.add_beneficiary(self.member, kind="MOBILE_MONEY", network="MPESA",
                                                    identifier="0712345678", holder_name="Asha Juma")

    def test_parallel_submissions_create_one_open_request(self):
        results = run_threads(4, lambda i: services.create_demo_withdrawal(
            self.root, self.member, self.beneficiary, amount=10_000, idempotency_key=f"token-{i}-abcdef"))
        created = [r for r in results if isinstance(r, DemoWithdrawal)]
        self.assertEqual(len(created), 1, results)
        self.assertEqual(DemoWithdrawal.objects.count(), 1)

    def test_parallel_notices_complete_once(self):
        demo = services.create_demo_withdrawal(self.root, self.member, self.beneficiary, amount=10_000,
                                               idempotency_key="token-single-abc")
        third = make_user("third@example.com", role=User.Role.SUPER_ADMIN)
        demo = services.start_review(self.approver, demo, expected_status=demo.status)
        demo = services.approve(self.approver, demo, expected_status=demo.status)
        demo = services.approve(third, demo, expected_status=demo.status)
        demo = services.initiate_payout(self.root, demo)
        from apps.withdrawals.models import SandboxPayout

        SandboxPayout.objects.filter(reference=demo.payout_reference).update(status="SUCCEEDED", sequence=1)
        provider = SandboxPayoutProvider()
        results = run_threads(4, lambda i: services.process_payout_update(
            provider, f"evt-{i}", demo.payout_reference, kind="poll"))
        self.assertEqual(sum(1 for r in results if r == "ACCEPTED"), 1, results)
        self.assertEqual(demo.decisions.filter(action="completed").count(), 1)
