"""UX-02: the payment methods workspace.

The list is read-only; adding and editing are reviewed and explicitly confirmed before anything is
published; retiring and reactivating have their own confirmation pages. Publishing always creates a
new version, so issued instructions and pending claims keep the details their member was shown.
"""

from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.accounts.tests.helpers import User, make_user
from apps.audit.models import AuditEvent
from apps.payments import manual
from apps.payments.models import ManualMembershipPayment, ManualPaymentInstruction

from .manual_helpers import BUSINESS_NUMBER, RECIPIENT, ManualPaymentTestCase, console_publish, staff_with

LIST = reverse("administration:payment_instructions")
NEW = reverse("administration:payment_method_new")


def edit_url(row):
    return reverse("administration:payment_method_edit", args=[row.pk])


def retire_url(row):
    return reverse("administration:payment_method_retire", args=[row.pk])


def reactivate_url(row):
    return reverse("administration:payment_method_reactivate", args=[row.pk])


TIGO = {"preset": "OTHER", "method": "TIGO", "provider_label": "Tigo Pesa", "business_number": "654321",
        "recipient_name": "TEST TIGO RECIPIENT", "instructions": "1. Dial *150*01#\n2. Pay the exact amount"}


class ListPageTests(ManualPaymentTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.root)

    def test_active_cards_are_compact_and_the_page_has_no_forms(self):
        page = self.client.get(LIST)
        content = page.content.decode()
        main = content[content.index('<main id="content"'):content.index("</main>")]
        self.assertContains(page, "<h1 class=\"page-title\">Payment Methods</h1>", html=True)
        self.assertContains(page, "css/pages/administration/payment-methods.css")
        self.assertNotContains(page, "style=")
        self.assertNotIn("<form", main)  # Read-only: every change has its own confirmed page.
        self.assertContains(page, f'href="{NEW}"')
        self.assertContains(page, "Add Payment Method")
        self.assertContains(page, f'href="{reverse("administration:membership_payments")}"')
        self.assertContains(page, 'class="method-card"', count=1)
        for text in ("Airtel Money - Lipa", BUSINESS_NUMBER, RECIPIENT, ">Active<", f'href="{edit_url(self.instruction)}"'):
            self.assertContains(page, text)
        # The member steps are available but collapsed, and the old repeated warnings are gone.
        self.assertIn('<details class="method-card__steps">', content)
        for removed in ("Check every receiving number and registered recipient before publishing",
                        "Members' payments are never approved automatically", "Publish payment details"):
            self.assertNotContains(page, removed)

    def test_copy_number_button_carries_the_exact_number(self):
        page = self.client.get(LIST)
        status_id = f"pn-status-{self.instruction.pk}"
        self.assertContains(page, f'data-copy-value="{BUSINESS_NUMBER}"')
        self.assertContains(page, f'aria-describedby="{status_id}"')
        self.assertContains(page, f'id="{status_id}" role="status"')
        script = (Path(settings.BASE_DIR) / "static/js/shared-ui.js").read_text(encoding="utf-8")
        self.assertIn('navigator.clipboard.writeText(button.getAttribute("data-copy-value"))', script)

    def test_retired_methods_and_history_are_collapsed_with_safe_reactivation(self):
        tigo = manual.publish_instruction(self.root, method="TIGO", provider_label="Tigo Pesa",
                                          business_number="654321", recipient_name="TEST TIGO RECIPIENT")
        manual.retire_instruction(self.root, "TIGO")
        old_airtel = self.instruction
        manual.publish_instruction(self.root, business_number="777777", recipient_name=RECIPIENT)
        page = self.client.get(LIST)
        content = page.content.decode()
        self.assertIn('<details class="methods-panel">\n  <summary>Retired method (1)</summary>', content)
        self.assertIn("Version history (3)", content)
        self.assertNotIn("<details class=\"methods-panel\" open", content)
        self.assertContains(page, f'href="{reactivate_url(tigo)}"')
        self.assertNotContains(page, f'href="{reactivate_url(old_airtel)}"')  # Replaced, not retired.
        self.assertContains(page, ">Replaced<")
        self.assertNotIn("<table", content)  # Rows, not a table that scrolls sideways on phones.

    def test_empty_state_offers_adding_a_method(self):
        manual.retire_instruction(self.root)
        page = self.client.get(LIST)
        self.assertContains(page, "No active payment methods.")
        self.assertContains(page, f'href="{NEW}"', count=2)


class AddMethodTests(ManualPaymentTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.root)

    def test_form_is_ordered_with_the_provider_code_under_advanced(self):
        page = self.client.get(NEW)
        content = page.content.decode()
        order = [content.index(f'name="{name}"') for name in
                 ("preset", "provider_label", "business_number", "recipient_name", "instructions", "method")]
        self.assertEqual(order, sorted(order))
        self.assertIn('<details class="method-form__advanced" data-advanced>', content)  # Closed by default.
        self.assertContains(page, '<option value="" selected>Choose a provider</option>', html=True)
        self.assertContains(page, "js/payment-method-form.js")
        self.assertContains(page, "Nothing is published until you confirm on the next screen.")

    def test_review_then_confirm_publishes_once(self):
        before = ManualPaymentInstruction.objects.count()
        review = self.client.post(NEW, {"step": "review", **TIGO})
        self.assertEqual(review.status_code, 200)
        self.assertContains(review, "Check and publish")
        self.assertContains(review, "This adds Tigo Pesa as a new way to pay.")
        self.assertContains(review, 'name="details_checked"')
        self.assertContains(review, '<input type="hidden" name="business_number" value="654321"')
        self.assertEqual(ManualPaymentInstruction.objects.count(), before)  # Reviewing saves nothing.

        refused = self.client.post(NEW, {"step": "confirm", "expected_active": "", **TIGO})
        self.assertContains(refused, "Confirm that you checked the details", status_code=400)
        self.assertIsNone(manual.active_instruction("TIGO"))

        back = self.client.post(NEW, {"step": "edit", **TIGO})
        self.assertContains(back, 'value="654321"')
        self.assertContains(back, "Review details")

        response = console_publish(self.client, TIGO, review=False)
        self.assertRedirects(response, LIST, fetch_redirect_response=False)
        tigo = manual.active_instruction("TIGO")
        self.assertEqual((tigo.provider_label, tigo.business_number, tigo.recipient_name, tigo.created_by),
                         ("Tigo Pesa", "654321", "TEST TIGO RECIPIENT", self.root))
        self.assertTrue(AuditEvent.objects.filter(action="membership.payment_instructions.published",
                                                  metadata__method="TIGO").exists())
        self.assertContains(self.client.get(LIST), "Tigo Pesa payment details published.")

    def test_validation_feedback(self):
        cases = {
            "no provider": ({"preset": "", "method": ""}, "Choose a provider."),
            "other without code": ({"preset": "OTHER", "method": ""}, "Enter a provider code"),
            "bad code": ({"preset": "OTHER", "method": "9x"}, "Use 2 to 20 uppercase letters"),
            "letters in number": ({"business_number": "12AB"}, "digits only"),
            "short recipient": ({"recipient_name": "X"}, "Enter the recipient name"),
        }
        for name, (overrides, message) in cases.items():
            with self.subTest(name):
                response = self.client.post(NEW, {"step": "review", **TIGO, **overrides})
                self.assertContains(response, message, status_code=400)
                self.assertContains(response, "error-summary", status_code=400)
                if overrides.get("preset") == "OTHER":
                    self.assertContains(response, '<details class="method-form__advanced" open data-advanced>',
                                        status_code=400)
        self.assertIsNone(manual.active_instruction("TIGO"))

    def test_all_problems_are_reported_in_one_submission(self):
        response = self.client.post(NEW, {"step": "review", "preset": "OTHER", "method": "",
                                          "business_number": "12AB", "recipient_name": ""})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.context["form"].errors), {"method", "business_number", "recipient_name"})

    def test_adding_never_replaces_an_active_provider(self):
        for data in ({"preset": "AIRTEL_LIPA"},
                     {"preset": "OTHER", "method": "airtel_lipa", "provider_label": "Airtel again"}):
            with self.subTest(data=data):
                response = self.client.post(NEW, {"step": "review", "business_number": "999999",
                                                  "recipient_name": "SOMEONE ELSE", **data})
                self.assertContains(response, "already has an active payment method", status_code=400)
                response = console_publish(self.client, {"business_number": "999999",
                                                         "recipient_name": "SOMEONE ELSE", **data}, review=False)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(manual.active_instruction().business_number, BUSINESS_NUMBER)
        self.assertEqual(ManualPaymentInstruction.objects.count(), 1)

    def test_a_concurrent_add_is_refused_at_confirmation(self):
        self.client.post(NEW, {"step": "review", **TIGO})
        other = manual.publish_instruction(self.root, method="TIGO", provider_label="Tigo Pesa",
                                           business_number="111111", recipient_name="OTHER ADMIN")
        response = console_publish(self.client, TIGO, review=False)
        self.assertEqual(response.status_code, 400)  # The add form now sees an active provider.
        self.assertEqual(manual.active_instruction("TIGO").pk, other.pk)
        with self.assertRaises(manual.InvalidState):  # And the service refuses it under its lock.
            manual.publish_instruction(self.root, method="TIGO", business_number="222222",
                                       recipient_name="LATE", expected_active=None)


class EditMethodTests(ManualPaymentTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.root)
        self.data = {"provider_label": "Airtel Money - Lipa", "business_number": "765432",
                     "recipient_name": "CLIPCASH LTD", "instructions": "Dial *150*60#."}

    def test_edit_shows_what_changes_and_keeps_the_provider_code(self):
        page = self.client.get(edit_url(self.instruction))
        self.assertContains(page, "Edit Airtel Money - Lipa")
        self.assertContains(page, f'value="{BUSINESS_NUMBER}"')
        self.assertNotContains(page, 'name="preset"')
        self.assertNotContains(page, 'name="method"')
        review = self.client.post(edit_url(self.instruction), {"step": "review", **self.data})
        self.assertContains(review, "What changes")
        self.assertContains(review, BUSINESS_NUMBER)
        self.assertContains(review, "765432")
        self.assertNotContains(review, "Payment steps for members</span>")  # Unchanged fields are not listed.
        self.assertContains(review, f'name="expected_active" value="{self.instruction.pk}"')

    def test_unchanged_details_are_not_republished(self):
        response = self.client.post(edit_url(self.instruction), {
            "step": "review", "provider_label": self.instruction.provider_label,
            "business_number": BUSINESS_NUMBER, "recipient_name": RECIPIENT, "instructions": "Dial *150*60#."})
        self.assertContains(response, "Nothing changed.", status_code=400)
        self.assertEqual(ManualPaymentInstruction.objects.count(), 1)

    def test_publishing_an_edit_keeps_history_quotes_and_pending_claims(self):
        claim = self.submit(reference="MPHIST12345")
        quote_id = claim.quote_id
        response = console_publish(self.client, self.data, self.instruction)
        self.assertRedirects(response, LIST)
        new = manual.active_instruction()
        self.assertNotEqual(new.pk, self.instruction.pk)
        self.assertEqual((new.method, new.business_number), (self.instruction.method, "765432"))
        old = ManualPaymentInstruction.objects.get(pk=self.instruction.pk)
        self.assertEqual((old.is_active, old.business_number, old.recipient_name), (False, BUSINESS_NUMBER, RECIPIENT))
        claim.refresh_from_db()
        self.assertEqual((claim.instruction_id, claim.quote_id), (self.instruction.pk, quote_id))
        self.assertEqual(claim.quote.instruction_id, self.instruction.pk)
        # The pending claim is still reviewed against the details its member was shown.
        self.client.force_login(self.verifier)
        review = self.client.get(reverse("administration:membership_payment", args=[claim.pk]))
        self.assertContains(review, f"Lipa {BUSINESS_NUMBER}")
        self.assertTrue(manual.approve(self.verifier, claim, verified_reference="MPHIST12345",
                                       receipt_confirmed=True, recipient_confirmed=True, amount_confirmed=True)[1])

    def test_a_stale_edit_is_refused_without_publishing(self):
        self.client.post(edit_url(self.instruction), {"step": "review", **self.data})
        other = manual.publish_instruction(self.root, business_number="111111", recipient_name="OTHER ADMIN")
        # The reviewed version is no longer live, so its edit page no longer exists.
        self.assertEqual(console_publish(self.client, self.data, self.instruction, review=False).status_code, 404)
        # A forged confirmation against the new version with the old expectation is refused too.
        response = self.client.post(edit_url(other), {"step": "confirm", "details_checked": "on",
                                                      "expected_active": str(self.instruction.pk), **self.data})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(manual.active_instruction().pk, other.pk)
        with self.assertRaises(manual.InvalidState):
            manual.publish_instruction(self.root, business_number="222222", recipient_name="LATE",
                                       expected_active=self.instruction)

    def test_retired_and_replaced_versions_cannot_be_edited(self):
        manual.retire_instruction(self.root)
        self.assertEqual(self.client.get(edit_url(self.instruction)).status_code, 404)
        self.assertEqual(self.client.post(edit_url(self.instruction), {"step": "review", **self.data}).status_code, 404)


class RetireAndReactivateTests(ManualPaymentTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.root)

    def test_retire_asks_first_and_keeps_pending_claims_valid(self):
        claim = self.submit(reference="MPRETIRE123")
        page = self.client.get(retire_url(self.instruction))
        self.assertContains(page, "Retire Airtel Money - Lipa?")
        self.assertContains(page, "1 payment waiting for verification keeps these details")
        self.assertIsNotNone(manual.active_instruction())  # Opening the page changes nothing.
        self.assertRedirects(self.client.post(retire_url(self.instruction)), LIST)
        self.assertIsNone(manual.active_instruction())
        self.assertTrue(AuditEvent.objects.filter(action="membership.payment_instructions.retired").exists())
        self.assertEqual(self.client.get(retire_url(self.instruction)).status_code, 404)  # Already retired.
        # New members cannot pay; the waiting claim can still be confirmed.
        self.assertEqual(manual.purchase_state(), manual.PURCHASE_UNPUBLISHED)
        self.assertTrue(manual.approve(self.verifier, claim, verified_reference="MPRETIRE123",
                                       receipt_confirmed=True, recipient_confirmed=True, amount_confirmed=True)[1])
        self.assertEqual(ManualMembershipPayment.objects.get(pk=claim.pk).status, "APPROVED")

    def test_reactivate_republishes_the_same_details_as_a_new_version(self):
        manual.retire_instruction(self.root)
        page = self.client.get(reactivate_url(self.instruction))
        self.assertContains(page, "Reactivate Airtel Money - Lipa?")
        self.assertContains(page, BUSINESS_NUMBER)
        self.assertRedirects(self.client.post(reactivate_url(self.instruction)), LIST)
        active = manual.active_instruction()
        self.assertNotEqual(active.pk, self.instruction.pk)
        self.assertEqual((active.business_number, active.recipient_name), (BUSINESS_NUMBER, RECIPIENT))
        self.assertTrue(AuditEvent.objects.filter(action="membership.payment_instructions.enabled").exists())
        self.assertEqual(self.client.get(reactivate_url(active)).status_code, 404)  # Active: nothing to do.

    def test_replaced_versions_can_never_be_reactivated(self):
        replacement = manual.publish_instruction(self.root, business_number="777777", recipient_name=RECIPIENT)
        manual.retire_instruction(self.root)
        self.assertEqual(self.client.get(reactivate_url(self.instruction)).status_code, 404)
        self.assertEqual(self.client.post(reactivate_url(self.instruction)).status_code, 404)
        with self.assertRaises(manual.InvalidState):
            manual.enable_instruction(self.root, self.instruction)
        self.assertIsNone(manual.active_instruction())
        self.client.post(reactivate_url(replacement))
        self.assertEqual(manual.active_instruction().business_number, "777777")


class PermissionTests(ManualPaymentTestCase):
    def test_only_super_administrators_reach_any_payment_method_page(self):
        manual.retire_instruction(self.root)
        retired = self.instruction
        active = manual.publish_instruction(self.root, method="TIGO", provider_label="Tigo Pesa",
                                            business_number="654321", recipient_name="TEST TIGO RECIPIENT")
        pages = (LIST, NEW, edit_url(active), retire_url(active), reactivate_url(retired))
        verifier = staff_with("pm-verifier@example.com", "verify_manual_payments", "view_manual_payments")
        manager = make_user("pm-manager@example.com", role=User.Role.CONTENT_MANAGER)
        for user, status in ((verifier, 403), (manager, 403)):
            self.client.force_login(user)
            for url in pages:
                with self.subTest(user=user.email, url=url):
                    self.assertEqual(self.client.get(url).status_code, status)
                    self.assertEqual(self.client.post(url, {"step": "confirm", "details_checked": "on", **TIGO,
                                                            "business_number": "999999"}).status_code, status)
        for client_user in (self.member, None):
            if client_user:
                self.client.force_login(client_user)
            else:
                self.client.logout()
            for url in pages:
                response = self.client.post(url, {"step": "confirm", "details_checked": "on", **TIGO})
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("admin:login"), response["Location"])
        self.assertEqual(manual.active_instruction("TIGO").business_number, "654321")
        self.assertIsNone(manual.active_instruction("AIRTEL_LIPA"))
        self.assertEqual(ManualPaymentInstruction.objects.count(), 2)

    def test_the_list_accepts_no_changes(self):
        self.client.force_login(self.root)
        response = self.client.post(LIST, {"business_number": "765432", "recipient_name": "X LTD"})
        self.assertEqual(response.status_code, 405)
        response = self.client.post(LIST, {"action": "retire", "instruction_id": self.instruction.pk})
        self.assertEqual(response.status_code, 405)
        self.assertEqual(manual.active_instruction().pk, self.instruction.pk)

    def test_csrf_is_enforced_on_confirmation(self):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        client.force_login(self.root)
        self.assertEqual(console_publish(client, TIGO, review=False).status_code, 403)
        self.assertEqual(client.post(retire_url(self.instruction)).status_code, 403)
        self.assertIsNone(manual.active_instruction("TIGO"))
        self.assertIsNotNone(manual.active_instruction())


class ResponsiveMarkupTests(ManualPaymentTestCase):
    def test_pages_use_the_modular_stylesheet_and_no_inline_styles(self):
        self.client.force_login(self.root)
        css = (Path(settings.BASE_DIR) / "static/css/pages/administration/payment-methods.css").read_text("utf-8")
        self.assertIn("@media (max-width: 40rem)", css)
        self.assertLess(len(css.splitlines()), 500)
        for url in (LIST, NEW, edit_url(self.instruction), retire_url(self.instruction)):
            with self.subTest(url=url):
                page = self.client.get(url)
                self.assertContains(page, "css/pages/administration/payment-methods.css")
                self.assertNotContains(page, "style=")
                self.assertContains(page, 'name="viewport" content="width=device-width, initial-scale=1"')

    def test_form_labels_and_errors_are_linked_for_keyboard_and_screen_readers(self):
        self.client.force_login(self.root)
        response = self.client.post(NEW, {"step": "review", **TIGO, "business_number": "12AB"})
        self.assertContains(response, '<a href="#id_business_number">', status_code=400)
        self.assertContains(response, 'aria-invalid="true"', status_code=400)
        self.assertContains(response, '<label for="id_business_number"', status_code=400)


class ServiceTests(ManualPaymentTestCase):
    def test_clean_values_match_publishing_rules(self):
        values = manual.clean_instruction_values(method=" tigo ", provider_label="  Tigo   Pesa ",
                                                 business_number="65 43 21", recipient_name=" A  B ")
        self.assertEqual((values["method"], values["provider_label"], values["business_number"],
                          values["recipient_name"]), ("TIGO", "Tigo Pesa", "654321", "A B"))
        with self.assertRaises(ValidationError) as caught:
            manual.clean_instruction_values(method="x", business_number="1", recipient_name="")
        # The default display name of a one-letter code is too short as well.
        self.assertEqual(set(caught.exception.message_dict),
                         {"method", "provider_label", "business_number", "recipient_name"})
