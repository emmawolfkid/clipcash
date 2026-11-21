"""
PRD-04 acceptance: the manual membership payment workflow additions, with fictional records only.

Receipt evidence (type by content, size, private storage, owner/staff-only download, audited staff
access, immutable rows guarded in the database), the purchase gate (plans browsable, choosing closed
until manual payments are approved), provider presets with bank-transfer wording, and the deploy
check for the evidence location. The rest of the workflow (quotes, single-use references, approval
with independent checks, one ledger posting and one activation, concurrency, notifications) is
covered by test_manual, test_manual_methods, test_manual_concurrency and the PAY-02/PAY-03 suites.
"""

import os
import shutil
import tempfile
from pathlib import Path

from django.core.checks import run_checks
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, IntegrityError, transaction
from django.test import override_settings
from django.urls import reverse

from apps.accounts.tests.helpers import make_user
from apps.audit.models import AuditEvent
from apps.memberships.models import Subscription
from apps.payments import manual
from apps.payments.models import ManualMembershipPayment, ManualPaymentEvidence, ManualPaymentInstruction
from apps.wallets.models import JournalEntry

from .manual_helpers import ManualPaymentTestCase, User, airtel_reference, staff_with

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"0" * 64


def upload(content=PNG, name="receipt.png", content_type="image/png"):
    return SimpleUploadedFile(name, content, content_type=content_type)


class EvidenceTestCase(ManualPaymentTestCase):
    def setUp(self):
        super().setUp()
        self.root_dir = tempfile.mkdtemp(prefix="clipcash-evidence-test-")
        self.addCleanup(shutil.rmtree, self.root_dir, ignore_errors=True)
        override = self.settings(PAYMENTS_EVIDENCE_ROOT=self.root_dir)
        override.enable()
        self.addCleanup(override.disable)

    def stored_files(self):
        return sorted(str(path.relative_to(self.root_dir)).replace("\\", "/")
                      for path in Path(self.root_dir).rglob("*") if path.is_file())

    def submit_with(self, evidence, member=None, reference=None):
        subscription = self.choose(member)
        claim, _created = manual.submit_claim(
            subscription.member, subscription, amount=subscription.price_amount, payer_phone="0781234567",
            transaction_reference=reference or airtel_reference(), evidence=evidence)
        return claim

    def pay_page(self, client, subscription):
        return reverse("payments:manual_pay", args=[subscription.pk])


class EvidenceSubmissionTests(EvidenceTestCase):
    def test_member_attaches_a_receipt_from_the_payment_page(self):
        self.client.force_login(self.member)
        subscription = self.choose()
        page = self.client.get(reverse("payments:manual_pay", args=[subscription.pk]))
        self.assertContains(page, 'enctype="multipart/form-data"')
        response = self.client.post(reverse("payments:manual_pay", args=[subscription.pk]), {
            "quote": page.context["form"].initial["quote"], "amount": "5000", "payer_phone": "0781234567",
            "transaction_reference": airtel_reference(), "evidence": upload(JPEG, "photo.png", "image/png")})
        claim = ManualMembershipPayment.objects.get(member=self.member)
        self.assertRedirects(response, reverse("payments:manual_claim", args=[claim.pk]),
                             fetch_redirect_response=False)
        evidence = claim.evidence
        self.assertEqual((evidence.content_type, evidence.size, evidence.uploaded_by_id),
                         ("image/jpeg", len(JPEG), self.member.pk))  # Type from content, not name or browser.
        self.assertEqual(self.stored_files(), [evidence.storage_name])
        self.assertRegex(evidence.storage_name, rf"^{claim.pk.hex}/[0-9a-f]{{32}}\.jpg$")  # Random name; no "photo".
        self.assertContains(self.client.get(reverse("payments:manual_claim", args=[claim.pk])),
                            "Download the receipt you attached")

    def test_files_are_stored_outside_public_paths(self):
        from django.conf import settings

        claim = self.submit_with(upload(PDF, "r.pdf", "application/pdf"))
        stored = Path(self.root_dir, claim.evidence.storage_name).resolve()
        for public in (settings.STATIC_ROOT, settings.MEDIA_ROOT):
            self.assertFalse(str(stored).startswith(str(Path(public).resolve())))

    def test_wrong_type_empty_and_oversized_files_are_refused_and_nothing_is_kept(self):
        cases = {"type": upload(b"GIF89a" + b"\x00" * 20, "receipt.png"),  # A GIF renamed .png.
                 "script": upload(b"<script>alert(1)</script>", "receipt.pdf", "application/pdf"),
                 "empty": upload(b"", "empty.png")}
        for label, file in cases.items():
            with self.subTest(label), self.assertRaises(Exception) as raised:
                self.submit_with(file)
            self.assertIn("evidence", getattr(raised.exception, "message_dict", {}))
        with self.settings(PAYMENTS_EVIDENCE_MAX_BYTES=32), self.assertRaises(Exception) as raised:
            self.submit_with(upload(PNG))
        self.assertIn("evidence", raised.exception.message_dict)
        self.assertFalse(ManualMembershipPayment.objects.exists())  # Refused before any claim.
        self.assertEqual(self.stored_files(), [])

    def test_an_oversized_request_is_refused_before_it_is_read(self):
        self.client.force_login(self.member)
        subscription = self.choose()
        with self.settings(PAYMENTS_EVIDENCE_MAX_BYTES=1024):
            response = self.client.post(reverse("payments:manual_pay", args=[subscription.pk]), {
                "amount": "5000", "payer_phone": "0781234567", "transaction_reference": airtel_reference(),
                "evidence": upload(PNG + b"\x00" * 200_000)})
        self.assertEqual(response.status_code, 413)
        self.assertFalse(ManualMembershipPayment.objects.exists())

    def test_a_duplicate_reference_stores_no_file(self):
        reference = airtel_reference()
        self.submit_with(upload(PNG), reference=reference)
        other = make_user("other@example.com")
        with self.assertRaises(Exception) as raised:
            self.submit_with(upload(JPEG, "x.jpg", "image/jpeg"), member=other, reference=reference)
        self.assertIn("transaction_reference", raised.exception.message_dict)
        self.assertEqual(len(self.stored_files()), 1)  # Only the first claim's file.

    def test_repeating_the_same_submission_keeps_one_claim_and_one_file(self):
        subscription = self.choose()
        reference = airtel_reference()
        kwargs = {"amount": subscription.price_amount, "payer_phone": "0781234567", "transaction_reference": reference}
        first, created = manual.submit_claim(self.member, subscription, evidence=upload(PNG), **kwargs)
        again, created_again = manual.submit_claim(self.member, subscription, evidence=upload(JPEG, "j.jpg"), **kwargs)
        self.assertEqual((first.pk, created, created_again), (again.pk, True, False))
        self.assertEqual(len(self.stored_files()), 1)


class EvidenceAccessTests(EvidenceTestCase):
    def setUp(self):
        super().setUp()
        self.claim = self.submit_with(upload(PDF, "r.pdf", "application/pdf"))
        self.url = reverse("payments:manual_evidence", args=[self.claim.pk])

    def test_the_member_downloads_their_own_receipt_safely(self):
        self.client.force_login(self.member)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn(self.claim.reference, response["Content-Disposition"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(b"".join(response.streaming_content), PDF)
        self.assertFalse(AuditEvent.objects.filter(action="membership.manual_payment.evidence_viewed").exists())

    def test_nobody_else_sees_it_and_staff_access_is_audited(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)  # Anonymous: to sign in.
        self.client.force_login(make_user("other@example.com"))
        self.assertEqual(self.client.get(self.url).status_code, 404)  # Another member: not even its existence.
        self.client.force_login(make_user("cm@example.com", role=User.Role.CONTENT_MANAGER))
        self.assertEqual(self.client.get(self.url).status_code, 404)  # Staff without the payment permission.
        self.client.force_login(self.verifier)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        event = AuditEvent.objects.get(action="membership.manual_payment.evidence_viewed")
        self.assertEqual((event.actor_id, event.metadata), (self.verifier.pk, {"reference": self.claim.reference}))

    def test_staff_detail_shows_the_receipt_and_flags_a_reused_file(self):
        other = make_user("other@example.com")
        reused = self.submit_with(upload(PDF, "same.pdf", "application/pdf"), member=other)
        self.client.force_login(self.verifier)
        page = self.client.get(reverse("administration:membership_payment", args=[reused.pk]))
        self.assertContains(page, self.url.replace(str(self.claim.pk), str(reused.pk)))
        self.assertContains(page, "The same receipt file was attached to other submissions")
        self.assertContains(page, self.claim.reference)


class EvidenceGuardTests(EvidenceTestCase):
    def test_rows_are_immutable_and_cannot_be_deleted(self):
        claim = self.submit_with(upload(PNG))
        for statement in (lambda: ManualPaymentEvidence.objects.filter(pk=claim.evidence.pk).update(size=1),
                          lambda: ManualPaymentEvidence.objects.filter(pk=claim.evidence.pk).delete()):
            with self.subTest(statement), self.assertRaises(DatabaseError), transaction.atomic():
                statement()

    def test_only_the_members_own_pending_claim_accepts_evidence(self):
        claim = self.submit(reference=airtel_reference())
        values = {"storage_name": f"{claim.pk.hex}/{'a' * 32}.png", "content_type": "image/png", "size": 10,
                  "sha256": "0" * 64}
        with self.assertRaises((IntegrityError, DatabaseError)), transaction.atomic():
            ManualPaymentEvidence.objects.create(claim=claim, uploaded_by=self.verifier, **values)  # Not the member.
        self.approve(claim)
        with self.assertRaises((IntegrityError, DatabaseError)), transaction.atomic():
            ManualPaymentEvidence.objects.create(claim=claim, uploaded_by=self.member, **values)  # Not pending.

    def test_approval_with_evidence_posts_and_activates_exactly_once(self):
        claim = self.submit_with(upload(PNG))
        approved, changed = self.approve(claim)
        again, changed_again = self.approve(ManualMembershipPayment.objects.get(pk=claim.pk))
        self.assertEqual((approved.status, changed, changed_again), (ManualMembershipPayment.Status.APPROVED, True, False))
        self.assertEqual(JournalEntry.objects.filter(source_id=str(claim.pk)).count(), 1)
        self.assertEqual(Subscription.objects.get(pk=claim.subscription_id).status, Subscription.Status.ACTIVE)
        self.assertTrue(ManualPaymentEvidence.objects.filter(claim=claim).exists())  # Kept with the history.


class PurchaseGateTests(ManualPaymentTestCase):
    @override_settings(MANUAL_MEMBERSHIP_PAYMENTS_ENABLED=False)
    def test_plans_can_be_browsed_but_not_chosen_until_payments_open(self):
        for member in (self.member, User.objects.create_user(None, "Correct-Horse-Battery-9", username="baraka",
                                                             status=User.Status.ACTIVE)):
            with self.subTest(member.sign_in_identifier):
                self.client.force_login(member)
                self.assertEqual(self.client.get(reverse("memberships:plans")).status_code, 200)
                detail = self.client.get(reverse("memberships:plan_detail", args=[self.plan.code]))
                self.assertContains(detail, "Membership payments are not open yet")
                self.assertNotContains(detail, "Choose this plan")
                response = self.client.post(reverse("memberships:choose", args=[self.plan.code]), follow=True)
                self.assertContains(response, "Membership payments are not open yet")
        self.assertFalse(Subscription.objects.exists())

    def test_choosing_opens_once_payments_are_approved_and_published(self):
        self.client.force_login(self.member)
        self.assertContains(self.client.get(reverse("memberships:plan_detail", args=[self.plan.code])),
                            "Choose this plan")
        response = self.client.post(reverse("memberships:choose", args=[self.plan.code]))
        subscription = Subscription.objects.get(member=self.member)
        self.assertRedirects(response, reverse("payments:manual_pay", args=[subscription.pk]),
                             fetch_redirect_response=False)

    def test_retiring_every_payment_method_closes_choosing_again(self):
        manual.retire_instruction(self.root, "AIRTEL_LIPA")
        self.client.force_login(self.member)
        self.client.post(reverse("memberships:choose", args=[self.plan.code]))
        self.assertFalse(Subscription.objects.exists())

    def test_live_gateways_rewards_and_withdrawals_stay_locked(self):
        from apps.core import features

        self.assertEqual([key for key in features.FINANCIAL_FEATURES if features.is_enabled(key)], [])
        from django.conf import settings

        self.assertEqual(settings.PAYMENTS_MODE, "disabled")  # MANUAL_ON: the production setting.


class PresetTests(ManualPaymentTestCase):
    def publish(self, **data):
        self.client.force_login(self.root)
        return self.client.post(reverse("administration:payment_instructions"), {
            "business_number": "0123456789", "recipient_name": "CLIPCASH TEST LTD", "instructions": "", **data})

    def test_presets_fill_code_and_name_but_never_the_account(self):
        page = self.client_class()
        page.force_login(self.root)
        form_page = page.get(reverse("administration:payment_instructions"))
        for name in ("Vodacom M-Pesa", "Airtel Money - Lipa", "Mixx by Yas", "Bank transfer"):
            self.assertContains(form_page, name)
        self.publish(preset="MPESA")
        mpesa = ManualPaymentInstruction.objects.get(method="MPESA", is_active=True)
        self.assertEqual((mpesa.provider_label, mpesa.business_number), ("Vodacom M-Pesa", "0123456789"))
        self.publish(preset="BANK_TRANSFER", provider_label="CRDB Bank transfer")
        bank = ManualPaymentInstruction.objects.get(method="BANK_TRANSFER", is_active=True)
        self.assertEqual(bank.provider_label, "CRDB Bank transfer")  # A typed name wins over the preset.
        self.assertTrue(bank.is_bank)
        self.assertTrue(AuditEvent.objects.filter(action="membership.payment_instructions.published",
                                                  metadata__method="BANK_TRANSFER").exists())

    def test_only_super_administrators_manage_payment_details(self):
        self.client.force_login(self.verifier)
        response = self.client.post(reverse("administration:payment_instructions"), {
            "preset": "MPESA", "business_number": "0123456789", "recipient_name": "X LTD"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ManualPaymentInstruction.objects.filter(method="MPESA").exists())

    def test_bank_transfer_wording_for_members(self):
        self.publish(preset="BANK_TRANSFER", provider_label="CRDB Bank transfer")
        bank = ManualPaymentInstruction.objects.get(method="BANK_TRANSFER", is_active=True)
        subscription = self.choose()
        quote = manual.issue_instructions(self.member, subscription, bank)
        self.client.force_login(self.member)
        page = self.client.get(reverse("payments:manual_pay", args=[subscription.pk]) + f"?quote={quote.pk}")
        self.assertContains(page, "Account number")
        self.assertContains(page, "Your phone number")


class EvidenceStorageCheckTests(ManualPaymentTestCase):
    def ids(self):
        return {message.id for message in run_checks(include_deployment_checks=True)}

    def test_unsafe_locations_fail_the_deploy_check(self):
        from django.conf import settings

        for root in (os.path.join(str(settings.STATIC_ROOT), "evidence"), os.path.join(str(settings.MEDIA_ROOT), "e"),
                     os.path.join(str(settings.BASE_DIR), "private", "payment_evidence"), "relative/path"):
            with self.subTest(root), self.settings(PAYMENTS_EVIDENCE_ROOT=root):
                self.assertIn("payments.E005", self.ids())
        with self.settings(PAYMENTS_EVIDENCE_ROOT=os.path.join(tempfile.gettempdir(), "clipcash-private", "evidence")):
            self.assertNotIn("payments.E005", self.ids())

    @override_settings(MANUAL_MEMBERSHIP_PAYMENTS_ENABLED=False, PAYMENTS_EVIDENCE_ROOT="relative/path")
    def test_not_checked_while_manual_payments_are_off(self):
        self.assertNotIn("payments.E005", self.ids())
