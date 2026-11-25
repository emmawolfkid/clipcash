"""Configurable manual methods: frozen quotes, provider receipt namespaces and real DB guards."""
from io import StringIO
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.urls import reverse

from apps.accounts.tests.helpers import make_user
from apps.audit.models import AuditEvent
from apps.core.tests import test_mariadb_port as port
from apps.memberships import services as memberships
from apps.memberships.models import Subscription
from apps.payments import manual
from apps.payments.models import ManualMembershipPayment as Claim, ManualPaymentInstruction as Instruction, ManualPaymentQuote as Quote
from apps.payments.tests import test_manual_concurrency as concurrent
from apps.wallets.models import JournalEntry, JournalLine
from apps.wallets import services as ledger
from .manual_helpers import ManualPaymentTestCase, MANUAL_ON, BUSINESS_NUMBER, RECIPIENT, console_publish


class ConfigurableMethodTests(ManualPaymentTestCase):
    def setUp(self):
        super().setUp()
        self.tigo = manual.publish_instruction(self.root, method="TIGO", provider_label="Tigo Pesa",
                                               business_number="654321", recipient_name="TEST TIGO RECIPIENT")

    def claim_for(self, member=None, instruction=None, reference="SAME123456"):
        member = member or self.member
        subscription = self.choose(member)
        quote = manual.issue_instructions(member, subscription, instruction or self.tigo)
        return manual.submit_claim(member, subscription, quote=quote, amount=quote.amount,
                                   payer_phone="0781234567", transaction_reference=reference)[0]

    def verify(self, claim, **changes):
        values = dict(verified_amount=claim.amount, verified_reference=claim.transaction_reference,
                      receipt_confirmed=True, recipient_confirmed=True,
                      verified_receiving_number=claim.instruction.business_number,
                      verified_recipient_name=claim.instruction.recipient_name)
        values.update(changes)
        return manual.approve(self.verifier, claim, **values)

    def test_two_providers_can_use_the_same_receipt_but_each_only_once(self):
        airtel = self.claim_for(instruction=self.instruction)
        tigo = self.claim_for(make_user("tigo@example.com"))
        self.verify(airtel)
        self.verify(tigo)
        self.assertEqual(set(JournalEntry.objects.values_list("external_reference", flat=True)),
                         {"SAME123456", "TIGO:SAME123456"})
        self.assertEqual(set(Subscription.objects.values_list("payment_reference", flat=True)),
                         {"AIRTEL-LIPA:SAME123456", "MANUAL:TIGO:SAME123456"})
        self.assertEqual(ledger.get_platform_account("CASH").balance, 10000)
        with self.assertRaises(ValidationError):
            self.claim_for(make_user("duplicate@example.com"))

    def test_issued_number_label_amount_and_recipient_survive_edit(self):
        pending = self.choose()
        quote = manual.issue_instructions(self.member, pending, self.tigo)
        updated = manual.publish_instruction(self.root, method="TIGO", provider_label="Tigo updated",
                                             business_number="777777", recipient_name="NEW TEST RECIPIENT")
        claim = manual.submit_claim(self.member, pending, quote=quote.pk, amount=5000,
                                    payer_phone="0781234567", transaction_reference="EDIT123456")[0]
        self.assertEqual(claim.instruction_id, self.tigo.pk)
        self.assertEqual((claim.instruction.provider_label, claim.instruction.business_number,
                          claim.instruction.recipient_name, claim.amount),
                         ("Tigo Pesa", "654321", "TEST TIGO RECIPIENT", 5000))
        with self.assertRaises(ValidationError):
            self.verify(claim, verified_receiving_number=updated.business_number)
        with self.assertRaises(ValidationError):
            self.verify(claim, verified_recipient_name=updated.recipient_name)
        self.assertFalse(JournalEntry.objects.exists())
        self.assertTrue(self.verify(claim)[1])
        newer_member = make_user("new-number@example.com")
        with self.assertRaises(ValidationError):
            self.claim_for(newer_member, updated, "EDIT123456")

    def test_retired_pending_claim_is_reviewable_and_enable_keeps_history(self):
        claim = self.claim_for(reference="RETIRED123")
        manual.retire_instruction(self.root, "TIGO")
        with self.assertRaises(manual.Unavailable):
            manual.issue_instructions(self.member, claim.subscription, self.tigo)
        self.assertTrue(self.verify(claim)[1])
        self.assertFalse(self.verify(claim)[1])
        enabled = manual.enable_instruction(self.root, self.tigo)
        self.assertNotEqual(enabled.pk, self.tigo.pk)
        self.assertEqual(enabled.method, "TIGO")
        claim.refresh_from_db()
        self.assertEqual(claim.instruction_id, self.tigo.pk)
        self.assertTrue(AuditEvent.objects.filter(action="membership.payment_instructions.enabled").exists())

    def test_quote_and_claim_cannot_be_rebound_or_forged(self):
        claim = self.claim_for()
        for model, key, change in (
            (Quote, claim.quote_id, {"amount": 1}),
            (Quote, claim.quote_id, {"instruction": self.instruction}),
            (Instruction, self.tigo.pk, {"provider_label": "Forged"}),
            (Claim, claim.pk, {"quote": None}),
        ):
            with self.subTest(change=change), self.assertRaises(DatabaseError), transaction.atomic():
                model.objects.filter(pk=key).update(**change)
        other = make_user("other-quote@example.com")
        pending = self.choose(other)
        with self.assertRaises(ValidationError):
            manual.submit_claim(other, pending, quote=claim.quote_id, amount=5000,
                                payer_phone="0781234567", transaction_reference="WRONG12345")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Quote.objects.create(subscription=pending, instruction=self.tigo, amount=1, currency="TZS")
        with self.assertRaises(DatabaseError), transaction.atomic():
            Quote.objects.filter(pk=claim.quote_id).delete()

    def test_legacy_airtel_claim_without_quote_stays_valid(self):
        pending = self.choose()
        claim = Claim.objects.create(member=self.member, subscription=pending, instruction=self.instruction,
                                     plan_name=pending.plan_name, amount=5000, currency="TZS",
                                     payer_phone="+255781234567", transaction_reference="LEGACY12345",
                                     reference_key="LEGACY12345")
        self.assertIsNone(claim.quote_id)
        self.verify(claim)
        self.assertEqual(JournalEntry.objects.get().external_reference, "LEGACY12345")
        pending.refresh_from_db()
        self.assertEqual(pending.payment_reference, "AIRTEL-LIPA:LEGACY12345")

    def test_approval_failures_roll_back_every_stage_and_retry_posts_once(self):
        for stage in ("apps.payments.manual.memberships.activate_with_manual_payment",
                      "apps.payments.manual.record_business_event"):
            member = make_user(f"rollback-{stage.split('.')[-1]}@example.com")
            claim = self.claim_for(member, reference="RB" + str(member.pk).replace("-", "")[:16])
            cash = ledger.get_platform_account("CASH").balance
            with mock.patch(stage, side_effect=RuntimeError("forced test failure")), self.assertRaises(RuntimeError):
                self.verify(claim)
            claim.refresh_from_db()
            self.assertEqual(claim.status, "PENDING_VERIFICATION")
            self.assertIsNone(claim.ledger_entry_id)
            self.assertFalse(JournalEntry.objects.filter(source_id=str(claim.pk)).exists())
            self.assertEqual(ledger.get_platform_account("CASH").balance, cash)
            self.assertEqual(Subscription.objects.get(pk=claim.subscription_id).status, "PENDING")
            self.assertTrue(self.verify(claim)[1])
            self.assertFalse(self.verify(claim)[1])
            entry = JournalEntry.objects.get(source_id=str(claim.pk))
            lines = entry.lines.all()
            self.assertEqual(sum(line.amount for line in lines if line.side == "DEBIT"), 5000)
            self.assertEqual(sum(line.amount for line in lines if line.side == "CREDIT"), 5000)

    def test_member_chooses_provider_and_submits_original_quote_after_edit(self):
        self.client.force_login(self.member)
        pending = self.choose()
        url = reverse("payments:manual_pay", args=[pending.pk])
        page = self.client.get(url)
        self.assertIsNone(page.context["quote"])
        self.assertContains(page, "Tigo Pesa")
        response = self.client.post(url, {"action": "choose_method", "instruction": self.tigo.pk})
        self.assertEqual(response.status_code, 302)
        page = self.client.get(response.url)
        quote = page.context["quote"]
        self.assertContains(page, 'name="quote"')
        manual.publish_instruction(self.root, method="TIGO", provider_label="Tigo Pesa",
                                   business_number="888888", recipient_name="NEW NAME")
        response = self.client.post(url, {"quote": quote.pk, "amount": "5000", "payer_phone": "0781234567",
                                         "transaction_reference": "WEB1234567"})
        self.assertEqual(response.status_code, 302)
        claim = Claim.objects.get()
        self.assertEqual(claim.instruction_id, self.tigo.pk)
        manual.retire_instruction(self.root, "TIGO")
        self.client.force_login(self.verifier)
        review = reverse("administration:membership_payment", args=[claim.pk])
        page = self.client.get(review)
        self.assertContains(page, "654321")
        response = self.client.post(review, {"action": "approve", "amount_confirmed": "on",
                                            "verified_reference": "WEB1234567", "receipt_confirmed": "on",
                                            "recipient_confirmed": "on"})
        self.assertEqual(response.status_code, 302)
        claim.refresh_from_db()
        self.assertEqual(claim.status, "APPROVED")

    def test_admin_add_edit_retire_enable_and_permissions(self):
        retire = reverse("administration:payment_method_retire", args=[self.tigo.pk])
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(retire).status_code, 302)  # To the staff sign-in.
        self.assertIsNotNone(manual.active_instruction("TIGO"))
        self.client.force_login(self.root)
        response = console_publish(self.client, {"preset": "OTHER", "method": "HALO", "provider_label": "Halo Test",
                                                 "business_number": "444444", "recipient_name": "TEST HALO"})
        self.assertEqual(response.status_code, 302)
        row = manual.active_instruction("HALO")
        # Editing keeps the provider code: a posted code is ignored.
        response = console_publish(self.client, {"method": "CHANGED", "provider_label": "Halo Edited",
                                                 "business_number": "555555", "recipient_name": "EDITED TEST HALO"},
                                   row)
        self.assertEqual(response.status_code, 302)
        edited = manual.active_instruction("HALO")
        self.assertEqual(edited.business_number, "555555")
        self.assertIsNone(manual.active_instruction("CHANGED"))
        self.client.post(reverse("administration:payment_method_retire", args=[edited.pk]))
        self.assertIsNone(manual.active_instruction("HALO"))
        self.client.post(reverse("administration:payment_method_reactivate", args=[edited.pk]))
        self.assertIsNotNone(manual.active_instruction("HALO"))
        self.assertEqual(manual.active_instructions().count(), 3)

    def test_malformed_or_foreign_instruction_ids_are_refused(self):
        self.client.force_login(self.root)
        for path in ("/manage/membership-payments/instructions/invalid/edit/",
                     "/manage/membership-payments/instructions/invalid/retire/"):
            self.assertEqual(self.client.get(path).status_code, 404)
            self.assertEqual(self.client.post(path).status_code, 404)
        self.client.force_login(self.member)
        pending = self.choose()
        url = reverse("payments:manual_pay", args=[pending.pk])
        self.assertEqual(self.client.post(url, {"action": "choose_method", "instruction": "invalid"}).status_code, 404)
        other = make_user("foreign-quote@example.com")
        foreign = self.claim_for(other).quote_id
        self.assertEqual(self.client.get(url + "?quote=" + str(foreign)).status_code, 404)


@port.on_mariadb_only
class RuntimeProviderConcurrencyTests(concurrent.ManualPaymentConcurrencyTests):
    # Reuse the owner-created fixtures; all operations below run as the restricted login.
    test_concurrent_approvals_activate_once = None
    test_concurrent_approve_and_reject_decide_once = None
    test_same_reference_from_many_members_is_accepted_once = None
    test_double_submission_by_one_member_creates_one_claim = None

    def setUp(self):
        super().setUp()
        self.tigo = manual.publish_instruction(self.root, method="TIGO", provider_label="Tigo Pesa",
                                               business_number="654321", recipient_name="TEST TIGO")

    def verify(self, claim):
        return manual.approve(self.verifiers[0], claim, verified_amount=5000,
                              verified_reference=claim.reference_key, receipt_confirmed=True,
                              recipient_confirmed=True, verified_receiving_number=claim.instruction.business_number,
                              verified_recipient_name=claim.instruction.recipient_name)

    def tigo_claim(self, member, reference):
        pending = memberships.choose_plan(member, self.plan)
        quote = manual.issue_instructions(member, pending, self.tigo)
        return manual.submit_claim(member, pending, quote=quote, amount=5000,
                                   payer_phone="0781234567", transaction_reference=reference)[0]

    def test_concurrent_approval_after_number_edit_and_retirement_posts_once(self):
        claim = self.tigo_claim(self.member, "CONCUR123456")
        manual.publish_instruction(self.root, method="TIGO", provider_label="Tigo New",
                                   business_number="999999", recipient_name="NEW TEST")
        manual.retire_instruction(self.root, "TIGO")
        results = port.RuntimeWorkersMixin.runtime_threads(5, lambda _: self.verify(claim))
        self.assertTrue(all(isinstance(result, tuple) for result in results), results)
        self.assertEqual(sum(changed for _, changed in results), 1)
        self.assertEqual(JournalEntry.objects.count(), 1)
        self.assertEqual(Subscription.objects.filter(status="ACTIVE").count(), 1)
        self.assertEqual(ledger.get_platform_account("CASH").balance, 5000)
        self.assertEqual(port.RuntimeWorkersMixin.runtime_threads(2, lambda _: self.verify(claim))[0][1], False)

    def test_concurrent_receipt_submission_after_number_edit_has_one_claim(self):
        original = self.tigo_claim(self.member, "REUSE123456")
        self.verify(original)
        updated = manual.publish_instruction(self.root, method="TIGO", provider_label="Tigo New",
                                              business_number="999999", recipient_name="NEW TEST")
        members = [make_user(f"reuse{i}@example.com") for i in range(3)]
        pairs = []
        for member in members:
            pending = memberships.choose_plan(member, self.plan)
            pairs.append((member, pending, manual.issue_instructions(member, pending, updated)))
        def submit(index):
            member, pending, quote = pairs[index]
            return manual.submit_claim(member, pending, quote=quote, amount=5000, payer_phone="0781234567",
                                       transaction_reference="REUSE123456")
        results = port.RuntimeWorkersMixin.runtime_threads(3, submit)
        self.assertTrue(all(isinstance(result, ValidationError) for result in results), results)
        self.assertEqual(Claim.objects.filter(method="TIGO", reference_key="REUSE123456").count(), 1)

    def test_concurrent_distinct_providers_same_receipt_keep_separate_balanced_postings(self):
        pending = memberships.choose_plan(self.member, self.plan)
        quote = manual.issue_instructions(self.member, pending, manual.active_instruction("AIRTEL_LIPA"))
        airtel = manual.submit_claim(self.member, pending, quote=quote, amount=5000,
                                     payer_phone="0781234567", transaction_reference="BOTH123456")[0]
        tigo = self.tigo_claim(make_user("concurrent-tigo@example.com"), "BOTH123456")
        results = port.RuntimeWorkersMixin.runtime_threads(2, lambda i: self.verify([airtel, tigo][i]))
        self.assertTrue(all(isinstance(result, tuple) and result[1] for result in results), results)
        self.assertEqual(JournalEntry.objects.count(), 2)
        self.assertEqual(Subscription.objects.filter(status="ACTIVE").count(), 2)
        self.assertEqual(ledger.get_platform_account("CASH").balance, 10000)
