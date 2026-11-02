from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone

from apps.accounts.tests.helpers import run_worker
from apps.advertising import services as advertising
from apps.audit.models import AuditEvent
from apps.core.jobs.services import enqueue
from apps.payments import services, simulator
from apps.payments.models import (
    MembershipActivationPreview,
    ProviderEvent,
    ReconciliationException,
    SandboxAdvertiserFunds,
    SandboxCampaignAllocation,
)
from apps.payments.providers import PaymentsDisabled

from .helpers import PaymentIntent, PaymentsTestCase, SandboxPosting, make_user, signed_body, token

S = PaymentIntent.Status


class IntentValidationTests(PaymentsTestCase):
    def test_deposit_amounts_and_authority(self):
        for amount in (0, -5, 1.5, Decimal("1000.5"), True, 999, 100_000_001, "abc"):
            with self.subTest(amount=amount), self.assertRaises(ValidationError):
                self.deposit(amount=amount)
        manager = make_user("mgr@brand.example")
        self.org.memberships.create(user=manager, role="MANAGER")
        for user in (manager, self.member, self.root):
            with self.subTest(user=user.email), self.assertRaises(services.NotAuthorized):
                self.deposit(user=user)
        pending = self.make_org(make_user("p@x.example"), "Pending")
        with self.assertRaises(services.InvalidState):
            self.deposit(org=pending, user=pending.memberships.get().user)
        self.assertTrue(AuditEvent.objects.filter(action="payments.action.denied").exists())

    def test_deposit_is_idempotent_and_pending(self):
        key = token()
        first = services.create_deposit_intent(self.owner, self.org, amount=20_000, idempotency_key=key)
        again = services.create_deposit_intent(self.owner, self.org, amount=20_000, idempotency_key=key)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual((first.status, first.environment, first.funds_domain),
                         (S.PENDING, "SANDBOX", "ADVERTISER_RESTRICTED_FUNDS"))
        self.assertTrue(first.reference.startswith("CC-"))
        self.assertTrue(first.provider_reference.startswith("SBX-"))
        with self.assertRaises(ValidationError):
            services.create_deposit_intent(self.owner, self.org, amount=30_000, idempotency_key=key)

    def test_membership_intent_uses_quoted_price(self):
        intent = self.membership_intent()
        self.assertEqual((intent.amount, intent.funds_domain), (self.plan.price_amount, "MEMBERSHIP_REVENUE"))
        with self.assertRaises(services.InvalidState):  # One open payment per membership.
            services.create_membership_intent(self.member, intent.subscription, idempotency_key=token())
        other = make_user("other@x.example")
        with self.assertRaises(services.NotFound):
            services.create_membership_intent(other, intent.subscription, idempotency_key=token())

    def test_database_rules(self):
        intent = self.deposit()
        bad_updates = [{"amount": 1}, {"currency": "USD"}, {"environment": "LIVE"}, {"purpose": "MEMBERSHIP"},
                       {"provider_reference": "SBX-0000000000000000"}, {"status": S.SETTLED}]
        for change in bad_updates:
            with self.subTest(change=change), self.assertRaises((DatabaseError, IntegrityError)), \
                    transaction.atomic():
                PaymentIntent.objects.filter(pk=intent.pk).update(**change)
        with self.assertRaises(DatabaseError), transaction.atomic():
            PaymentIntent.objects.filter(pk=intent.pk).delete()


class ReconciliationTests(PaymentsTestCase):
    def test_deposit_lifecycle_posts_exactly_once(self):
        intent = self.deposit(40_000)
        simulator.simulate(self.root, intent, "pay")
        intent.refresh_from_db()
        self.assertEqual((intent.status, intent.provider_amount), (S.RECONCILED, 40_000))
        self.assertEqual(services.funds_for(self.org)["received"], 0)  # Not settled yet.
        simulator.simulate(self.root, intent, "replay")  # Same notice again.
        simulator.simulate(self.root, intent, "settle")
        intent.refresh_from_db()
        self.assertEqual(intent.status, S.SETTLED)
        self.assertEqual(list(SandboxPosting.objects.filter(intent=intent).values_list("posting_type", flat=True)),
                         ["advertiser.funding_received", "payment.settled", "advertiser.funding_verified"])
        self.assertEqual(services.funds_for(self.org)["available"], 40_000)
        self.assertEqual(self.balances_total(), 0)
        self.assertTrue(ProviderEvent.objects.filter(outcome="DUPLICATE").exists())
        self.assertTrue(AuditEvent.objects.filter(action="payments.callback.duplicate").exists())

    def test_forged_and_malformed_callbacks_are_refused(self):
        intent = self.deposit()
        status, outcome = simulator.simulate(self.root, intent, "forged")
        self.assertEqual((status, outcome), (401, "signature_invalid"))
        body, _ = signed_body(intent.provider_reference, 9)
        self.assertEqual(services.receive_callback("sandbox", body, "")[0], 401)
        import json

        from apps.payments.providers import SandboxProvider

        evil = json.dumps({"event_id": "x:1", "provider_reference": intent.provider_reference,
                           "amount": 999_999, "status": "PAID"}).encode()
        self.assertEqual(services.receive_callback("sandbox", evil, SandboxProvider.sign(evil)), (401, "malformed"))
        self.assertEqual(services.receive_callback("sandbox", b"x" * 5000, "abc")[0], 413)
        intent.refresh_from_db()
        self.assertEqual(intent.status, S.PENDING)
        self.assertTrue(AuditEvent.objects.filter(action="payments.callback.refused").exists())

    def test_authentic_notice_without_payment_changes_nothing(self):
        intent = self.deposit()
        body, signature = signed_body(intent.provider_reference, 1)
        self.assertEqual(services.receive_callback("sandbox", body, signature), (200, "NO_CHANGE"))
        intent.refresh_from_db()
        self.assertEqual(intent.status, S.PENDING)  # A notice alone never creates money.

    def test_amount_mismatch_goes_to_review(self):
        intent = self.deposit(30_000)
        simulator.simulate(self.root, intent, "pay_wrong_amount")
        intent.refresh_from_db()
        self.assertEqual(intent.status, S.PROVIDER_CONFIRMED)
        exc = ReconciliationException.objects.get(intent=intent)
        self.assertEqual((exc.reason, exc.amount), ("amount_mismatch", 31_000))
        self.assertFalse(SandboxPosting.objects.exists())
        with self.assertRaises(services.InvalidState):
            services.resolve_exception(self.root, exc, action="retry", note="Check again")
        with self.assertRaises(services.NotAuthorized):
            services.resolve_exception(self.member, exc, action="reject_payment", note="Not me")
        services.resolve_exception(self.root, exc, action="reject_payment", note="Wrong amount paid")
        intent.refresh_from_db()
        self.assertEqual(intent.status, S.FAILED)
        self.assertEqual(services.funds_for(self.org)["received"], 0)

    def test_unknown_reference_and_closed_intent(self):
        intent = self.deposit()
        simulator.simulate(self.root, intent, "orphan")
        self.assertTrue(ReconciliationException.objects.filter(reason="unknown_reference").exists())
        services.cancel_intent(self.owner, intent)
        simulator.simulate(self.root, intent, "pay")
        self.assertTrue(ReconciliationException.objects.filter(intent=intent, reason="intent_closed").exists())
        intent.refresh_from_db()
        self.assertEqual(intent.status, S.CANCELLED)

    def test_failed_payment(self):
        intent = self.deposit()
        simulator.simulate(self.root, intent, "fail")
        intent.refresh_from_db()
        self.assertEqual(intent.status, S.FAILED)


class MembershipTests(PaymentsTestCase):
    def test_settled_membership_payment_is_only_previewed(self):
        intent = self.membership_intent()
        simulator.simulate(self.root, intent, "pay")
        simulator.simulate(self.root, intent, "settle")
        intent.refresh_from_db()
        preview = MembershipActivationPreview.objects.get(intent=intent)
        self.assertEqual(preview.decision, "WOULD_ACTIVATE")
        self.assertEqual((preview.would_end - preview.would_start).days, self.plan.duration_days)
        subscription = intent.subscription
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, "PENDING")  # Never activated by a sandbox payment.
        self.assertEqual(list(SandboxPosting.objects.filter(intent=intent).values_list("posting_type", flat=True)),
                         ["membership.payment_received", "payment.settled"])
        self.assertTrue(all(p.funds_domain == "MEMBERSHIP_REVENUE" for p in SandboxPosting.objects.filter(intent=intent)))
        # A second payment for the same membership is a duplicate, never a second period.
        second = services.create_membership_intent(self.member, subscription, idempotency_key=token())
        simulator.simulate(self.root, second, "pay")
        second.refresh_from_db()
        self.assertEqual(second.status, S.PROVIDER_CONFIRMED)
        self.assertTrue(ReconciliationException.objects.filter(intent=second, reason="duplicate_payment").exists())
        self.assertEqual(MembershipActivationPreview.objects.count(), 1)

    def test_member_cannot_pay_for_active_membership(self):
        subscription = self.active_membership()
        with self.assertRaises(services.InvalidState):
            services.create_membership_intent(self.member, subscription, idempotency_key=token())


class FundingTests(PaymentsTestCase):
    def setUp(self):
        super().setUp()
        self.campaign = self.running_campaign()
        self.settled_deposit(50_000)

    def test_allocation_limits_and_ownership(self):
        allocation = services.allocate(self.owner, self.org, self.campaign, amount=30_000)
        self.assertEqual(services.funds_for(self.org)["available"], 20_000)
        with self.assertRaises(services.InvalidState) as caught:
            services.allocate(self.owner, self.org, self.campaign, amount=1_000)
        self.assertEqual(caught.exception.code, "already_allocated")
        second = self.running_campaign(name="Second")
        with self.assertRaises(services.InvalidState) as caught:
            services.allocate(self.owner, self.org, second, amount=20_001)
        self.assertEqual(caught.exception.code, "insufficient_funds")
        rival_owner = make_user("rival@x.example")
        rival = services_org = self.make_org(rival_owner, "Rival")
        advertising.review_organization(self.root, rival, "approve", expected_status="PENDING")
        rival.refresh_from_db()
        with self.assertRaises(services.InvalidState) as caught:
            services.allocate(rival_owner, services_org, self.campaign, amount=1)
        self.assertEqual(caught.exception.code, "other_advertiser")
        with self.assertRaises(DatabaseError), transaction.atomic():
            SandboxCampaignAllocation.objects.filter(pk=allocation.pk).update(organization=rival)
        services.release_allocation(self.owner, allocation)
        services.release_allocation(self.owner, allocation)  # Idempotent.
        self.assertEqual(services.funds_for(self.org)["available"], 50_000)
        self.campaign.refresh_from_db()
        self.assertEqual((self.campaign.funding_status, self.campaign.financially_eligible), ("UNFUNDED", False))
        self.assertEqual(self.balances_total(), 0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SandboxAdvertiserFunds.objects.filter(organization=self.org).update(allocated=60_000)

    def test_refund_needs_two_people_and_unallocated_funds(self):
        intent = PaymentIntent.objects.get(purpose="ADVERTISER_DEPOSIT", status=S.SETTLED)
        services.allocate(self.owner, self.org, self.campaign, amount=10_000)
        with self.assertRaises(services.InvalidState) as caught:
            services.request_refund(self.root, intent, reason="Advertiser asked")
        self.assertEqual(caught.exception.code, "funds_committed")
        services.release_allocation(self.owner, SandboxCampaignAllocation.objects.get())
        refund = services.request_refund(self.root, intent, reason="Advertiser asked")
        self.assertEqual(services.funds_for(self.org)["frozen"], 50_000)  # Blocked from reuse.
        with self.assertRaises(services.NotAuthorized):
            services.decide_refund(self.root, refund, approve=True)
        services.decide_refund(self.approver, refund, approve=True, note="Approved")
        intent.refresh_from_db()
        self.assertEqual(intent.status, S.REFUNDED)
        self.assertEqual(services.funds_for(self.org), {"received": 50_000, "allocated": 0, "frozen": 0,
                                                        "refunded": 50_000, "available": 0})
        self.assertEqual(self.balances_total(), 0)
        self.assertEqual(services.check_funding(), 0)

    def test_chargeback_with_committed_funds_records_shortfall(self):
        intent = PaymentIntent.objects.get(purpose="ADVERTISER_DEPOSIT", status=S.SETTLED)
        allocation = services.allocate(self.owner, self.org, self.campaign, amount=45_000)
        simulator.simulate(self.root, intent, "chargeback")
        intent.refresh_from_db()
        dispute = intent.disputes.get()
        self.assertEqual((intent.status, dispute.frozen_amount, dispute.shortfall_amount), (S.DISPUTED, 5_000, 45_000))
        allocation.refresh_from_db()
        self.assertTrue(allocation.flagged)
        self.assertTrue(ReconciliationException.objects.filter(reason="funds_shortfall", amount=45_000).exists())
        self.assertEqual(services.funds_for(self.org)["available"], 0)
        with self.assertRaises(services.InvalidState):
            services.allocate(self.owner, self.org, self.running_campaign(name="Third"), amount=1)
        services.resolve_dispute(self.root, dispute, won=False, note="Chargeback upheld")
        intent.refresh_from_db()
        self.assertEqual(intent.status, S.REFUNDED)
        self.assertEqual(services.funds_for(self.org)["refunded"], 5_000)
        self.assertEqual(self.balances_total(), 0)
        self.assertEqual(services.check_funding(), 0)

    def test_dispute_won_restores_funds(self):
        intent = PaymentIntent.objects.get(purpose="ADVERTISER_DEPOSIT", status=S.SETTLED)
        simulator.simulate(self.root, intent, "chargeback")
        dispute = intent.disputes.get()
        services.resolve_dispute(self.approver, dispute, won=True, note="Evidence accepted")
        intent.refresh_from_db()
        self.assertEqual((intent.status, services.funds_for(self.org)["available"]), (S.SETTLED, 50_000))

    def test_funding_consistency_check(self):
        self.assertEqual(services.check_funding(), 0)
        SandboxAdvertiserFunds.objects.filter(organization=self.org).update(received=60_000)
        self.assertEqual(services.check_funding(), 1)
        self.assertTrue(ReconciliationException.objects.filter(reason="funding_inconsistent").exists())


class SandboxGateTests(PaymentsTestCase):
    def test_production_refuses_sandbox(self):
        intent = self.deposit()
        with override_settings(DEBUG=False, PAYMENTS_SANDBOX_ALLOWED=False):
            with self.assertRaises(PaymentsDisabled):
                self.deposit()
            with self.assertRaises(PaymentsDisabled):
                simulator.simulate(self.root, intent, "pay")
            body, signature = signed_body(intent.provider_reference, 1)
            with self.assertRaises(PaymentsDisabled):
                services.receive_callback("sandbox", body, signature)
            from apps.payments.checks import check_payments_mode, check_sandbox_in_production

            self.assertEqual([e.id for e in check_sandbox_in_production(None)], ["payments.E002"])
        with override_settings(PAYMENTS_MODE="live"):
            from apps.payments.checks import check_payments_mode

            self.assertEqual([e.id for e in check_payments_mode(None)], ["payments.E001"])
        with override_settings(PAYMENTS_MODE="disabled"), self.assertRaises(PaymentsDisabled):
            self.deposit()

    def test_simulator_requires_permission(self):
        intent = self.deposit()
        for actor in (self.owner, self.manager, self.member):
            with self.subTest(actor=actor.email), self.assertRaises(services.NotAuthorized):
                simulator.simulate(actor, intent, "pay")
        intent.refresh_from_db()
        self.assertEqual(intent.status, S.PENDING)


class JobAndFirewallTests(PaymentsTestCase):
    @override_settings(PAYMENTS_POLL_AFTER_SECONDS=0)
    def test_expiry_and_polling(self):
        stale = self.deposit()
        PaymentIntent.objects.filter(pk=stale.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(services.expire_intents(), 1)
        self.assertEqual(services.expire_intents(), 0)
        polled = self.deposit()
        record = self.provider_record(polled)
        record.status, record.sequence = "PAID", 1
        record.save()  # Provider changed state but its callback was lost.
        enqueue("payments.poll_pending", {})
        run_worker()
        enqueue("payments.poll_pending", {}, idempotency_key="again")
        run_worker()
        polled.refresh_from_db()
        self.assertEqual(polled.status, S.RECONCILED)
        self.assertEqual(SandboxPosting.objects.filter(intent=polled).count(), 1)

    def test_real_ledger_and_rewards_untouched(self):
        from apps.wallets.models import JournalEntry, JournalLine, LedgerAccount

        before = (JournalEntry.objects.count(), JournalLine.objects.count(), LedgerAccount.objects.count())
        campaign = self.running_campaign()
        self.settled_deposit(20_000)
        services.allocate(self.owner, self.org, campaign, amount=20_000)
        membership = self.membership_intent()
        simulator.simulate(self.root, membership, "pay")
        self.assertEqual((JournalEntry.objects.count(), JournalLine.objects.count(), LedgerAccount.objects.count()),
                         before)
        campaign.refresh_from_db()
        self.assertEqual(campaign.funding_status, "UNFUNDED")
        self.assertFalse(campaign.financially_eligible)
        # Membership and advertiser money never share a posting.
        for posting in SandboxPosting.objects.all():
            accounts = {posting.debit_account, posting.credit_account}
            membership_side = bool(accounts & {"MEMBERSHIP_DEFERRED_REVENUE", "MEMBERSHIP_REVENUE"})
            advertiser_side = bool(accounts & {"ADVERTISER_FUNDING_PENDING", "ADVERTISER_FUNDING_VERIFIED",
                                               "CAMPAIGN_RESTRICTED_FUNDING"})
            self.assertFalse(membership_side and advertiser_side, posting.posting_type)
        with self.assertRaises(DatabaseError), transaction.atomic():
            SandboxPosting.objects.update(amount=1)

    def test_audit_has_no_secrets(self):
        from apps.payments.providers import SandboxProvider

        intent = self.settled_deposit()
        simulator.simulate(self.root, intent, "forged")
        body, signature = signed_body(intent.provider_reference, 1)
        for event in AuditEvent.objects.filter(category="PAYMENTS"):
            text = str(event.metadata) + event.target_repr
            self.assertNotIn("0" * 64, text)  # The forged signature.
            self.assertNotIn(signature, text)
            self.assertNotIn(SandboxProvider.sign(b"x")[:16], text)
