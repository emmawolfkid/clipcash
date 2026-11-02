import uuid

from django.test import Client, override_settings
from django.urls import reverse

from apps.payments import simulator
from apps.payments.models import PaymentIntent

from .helpers import PaymentsTestCase, make_user, signed_body, token


class MemberAndAdvertiserPageTests(PaymentsTestCase):
    def test_member_starts_sandbox_payment(self):
        from apps.memberships import services as memberships

        subscription = memberships.choose_plan(self.member, self.plan)
        self.client.force_login(self.member)
        page = self.client.get(reverse("payments:history"))
        self.assertContains(page, "SANDBOX: simulated payments")
        self.assertContains(page, "Start sandbox payment")
        response = self.client.post(reverse("payments:start_membership", args=[subscription.pk]),
                                    {"form_token": token()})
        intent = PaymentIntent.objects.get()
        self.assertRedirects(response, reverse("payments:intent", args=[intent.pk]))
        detail = self.client.get(reverse("payments:intent", args=[intent.pk]))
        self.assertContains(detail, "Nothing is charged")
        for misleading in ("Payment successful", "Receipt", "Paid in full"):
            self.assertNotContains(detail, misleading)
        # Members cannot submit a status: the only browser action is cancel.
        self.client.post(reverse("payments:intent", args=[intent.pk]), {"status": "SETTLED"})
        intent.refresh_from_db()
        self.assertEqual(intent.status, "CANCELLED")

    def test_payments_are_private(self):
        intent = self.deposit()
        stranger = make_user("s@x.example")
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(reverse("payments:intent", args=[intent.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("payments:intent", args=[uuid.uuid4()])).status_code, 404)
        self.assertEqual(self.client.get(reverse("payments:funding", args=[self.org.pk])).status_code, 404)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("payments:intent", args=[intent.pk])).status_code, 200)

    def test_owner_funding_page_deposit_and_allocation(self):
        campaign = self.running_campaign()
        self.client.force_login(self.owner)
        url = reverse("payments:funding", args=[self.org.pk])
        page = self.client.get(url)
        self.assertContains(page, "Funding (sandbox)")
        self.assertContains(page, "Campaign funding status stays")
        self.client.post(url, {"action": "deposit", "amount": "25000", "form_token": token()})
        intent = PaymentIntent.objects.get(purpose="ADVERTISER_DEPOSIT")
        simulator.simulate(self.root, intent, "pay")
        simulator.simulate(self.root, intent, "settle")
        self.client.post(url, {"action": "allocate", "campaign": campaign.pk, "amount": "10000"})
        page = self.client.get(url)
        self.assertEqual(page.context["funds"]["available"], 15_000)
        bad = self.client.post(url, {"action": "deposit", "amount": "12.5", "form_token": token()}, follow=True)
        self.assertContains(bad, "whole number")
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.owner)
        self.assertEqual(csrf.post(url, {"action": "deposit", "amount": "5000", "form_token": token()}).status_code,
                         403)

    def test_callback_endpoint(self):
        intent = self.deposit()
        record = self.provider_record(intent)
        record.status, record.sequence = "PAID", 1
        record.save()
        body, signature = signed_body(intent.provider_reference, 1)
        url = reverse("payments:sandbox_callback")
        anonymous = Client(enforce_csrf_checks=True)  # Provider callbacks carry no CSRF token.
        self.assertEqual(anonymous.post(url, body, content_type="application/json",
                                        HTTP_X_SANDBOX_SIGNATURE="0" * 64).status_code, 401)
        ok = anonymous.post(url, body, content_type="application/json", HTTP_X_SANDBOX_SIGNATURE=signature)
        self.assertEqual(ok.status_code, 200)
        replay = anonymous.post(url, body, content_type="application/json", HTTP_X_SANDBOX_SIGNATURE=signature)
        self.assertEqual(replay.json()["outcome"], "DUPLICATE")
        intent.refresh_from_db()
        self.assertEqual(intent.status, "RECONCILED")
        self.assertEqual(anonymous.get(url).status_code, 405)
        with override_settings(DEBUG=False, PAYMENTS_SANDBOX_ALLOWED=False):
            self.assertEqual(anonymous.post(url, body, content_type="application/json",
                                            HTTP_X_SANDBOX_SIGNATURE=signature).status_code, 404)
            self.client.force_login(self.member)
            self.assertEqual(self.client.get(reverse("payments:history")).status_code, 404)


class ConsoleTests(PaymentsTestCase):
    def test_console_access_and_simulator(self):
        intent = self.deposit()
        for user in (self.manager, self.owner):
            self.client.force_login(user)
            self.assertIn(self.client.get(reverse("administration:payments")).status_code, (302, 403))
        self.client.force_login(self.root)
        for name in ("payments", "payment_exceptions", "payment_funding"):
            self.assertEqual(self.client.get(reverse(f"administration:{name}")).status_code, 200)
        url = reverse("administration:payment", args=[intent.pk])
        self.assertContains(self.client.get(url), "Sandbox provider simulator")
        self.client.post(url, {"action": "simulate", "simulate": "pay"})
        self.client.post(url, {"action": "simulate", "simulate": "settle"})
        intent.refresh_from_db()
        self.assertEqual(intent.status, "SETTLED")
        funding = self.client.get(reverse("administration:payment_funding"))
        self.assertEqual(funding.context["balance_total"], 0)

    def test_refund_flow_via_console(self):
        intent = self.settled_deposit()
        url = reverse("administration:payment", args=[intent.pk])
        self.client.force_login(self.root)
        self.client.post(url, {"action": "request_refund", "reason": "Advertiser request"})
        refund = intent.refunds.get()
        self.client.post(url, {"action": "decide_refund", "refund": refund.pk, "approve": "1"})
        refund.refresh_from_db()
        self.assertEqual(refund.status, "REQUESTED")  # Same person cannot approve.
        self.client.force_login(self.approver)
        self.client.post(url, {"action": "decide_refund", "refund": refund.pk, "approve": "1", "note": "OK"})
        intent.refresh_from_db()
        self.assertEqual(intent.status, "REFUNDED")

    def test_exception_queue(self):
        intent = self.deposit()
        simulator.simulate(self.root, intent, "pay_wrong_amount")
        self.client.force_login(self.root)
        queue = self.client.get(reverse("administration:payment_exceptions"))
        self.assertContains(queue, "Amount does not match")
        exception = intent.exceptions.get()
        self.client.post(reverse("administration:payment_exceptions"),
                         {"exception": exception.pk, "action": "reject_payment", "note": "Wrong amount"})
        intent.refresh_from_db()
        self.assertEqual(intent.status, "FAILED")
