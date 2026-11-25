"""UI-DASH-03 closure: manual payment notices follow the reader's language.

The stored (history and email) text stays the English rendering, in the same words as before, whatever
language the member or the verifier was using; the inbox shows the notice in the reader's language,
with dates using the language's month names. Staff notes are shown as written.
"""
from django.conf import settings
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.formats import date_format

from apps.notifications import inbox
from apps.notifications.models import Notification
from apps.payments import manual

from .manual_helpers import ManualPaymentTestCase


class PaymentNoticeLanguageTests(ManualPaymentTestCase):
    def notice(self, event):
        return Notification.objects.get(recipient=self.member, kind=f"membership.payment_{event}")

    def in_swahili(self, notice):
        with translation.override("sw"):
            return inbox.localize([notice])[0]

    def test_submitted_notice(self):
        with translation.override("sw"):  # The member's own language never changes the stored text.
            claim = self.submit()
        notice = self.notice("submitted")
        amount = manual.format_tzs(claim.amount)
        self.assertEqual(notice.title, "Payment verification requested")
        self.assertEqual(notice.message,
                         f"We received your request to verify your payment for {claim.plan_name} ({amount}, "
                         f"reference {claim.transaction_reference}). Your membership starts after our team "
                         "confirms the payment in the provider business records.")
        sw = self.in_swahili(notice)
        self.assertEqual(sw.title, "Uhakiki wa malipo umeombwa")
        self.assertIn(f"kumbukumbu {claim.transaction_reference}", sw.message)
        self.assertTrue(sw.message.startswith("Tumepokea ombi lako"))

    def test_approved_notice_and_date(self):
        claim = self.submit()
        with translation.override("sw"):  # A Swahili-speaking verifier: the member still gets the template.
            claim, _ = self.approve(claim)
        subscription = claim.subscription
        subscription.refresh_from_db()
        expires = timezone.localtime(subscription.expires_at).date()
        notice = self.notice("approved")
        self.assertEqual(notice.title, "Payment confirmed: membership active")
        self.assertEqual(notice.message,
                         f"Your payment of {manual.format_tzs(claim.amount)} (reference {claim.transaction_reference})"
                         f" was confirmed. Your {claim.plan_name} membership is active until {expires:%d %b %Y}.")
        sw = self.in_swahili(notice)
        with translation.override("sw"):
            sw_date = date_format(expires, "j F Y")
        self.assertEqual(sw.title, "Malipo yamethibitishwa: uanachama unatumika")
        self.assertTrue(sw.message.endswith(f"unatumika hadi {sw_date}."), sw.message)
        self.assertNotEqual(sw_date, f"{expires:%d %b %Y}")  # Month name comes from the Swahili catalogue.

    def test_rejected_notice_with_staff_note(self):
        claim = self.submit()
        manual.reject(self.verifier, claim, reason="NOT_FOUND", note="Not in statement yet.")
        notice = self.notice("rejected")
        self.assertEqual(notice.title, "Payment not confirmed")
        self.assertEqual(notice.message,
                         f"We could not confirm your payment for {claim.plan_name} (reference "
                         f"{claim.transaction_reference}): no matching transaction in the provider business "
                         "records. Not in statement yet. You can submit corrected details or contact support.")
        sw = self.in_swahili(notice)
        self.assertEqual(sw.title, "Malipo hayajathibitishwa")
        self.assertIn("hakuna muamala unaolingana kwenye kumbukumbu za biashara za mtoa huduma.", sw.message)
        self.assertIn("Not in statement yet. Unaweza kuwasilisha", sw.message)  # The note as written.

    def test_rejected_notice_without_note(self):
        claim = self.submit()
        manual.reject(self.verifier, claim, reason="AMOUNT_MISMATCH")
        notice = self.notice("rejected")
        self.assertTrue(notice.message.endswith(
            ": the amount received does not match the plan price. You can submit corrected details or contact "
            "support."), notice.message)
        self.assertIn("kiasi kilichopokelewa hakilingani na bei ya mpango. Unaweza", self.in_swahili(notice).message)

    def test_inbox_page_in_both_languages_and_history_kept(self):
        claim = self.submit()
        self.approve(claim)
        self.client.force_login(self.member)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "sw"
        self.assertContains(self.client.get(reverse("notifications:inbox")), "Malipo yamethibitishwa")
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        self.assertContains(self.client.get(reverse("notifications:inbox")), "Payment confirmed: membership active")
        stored = self.notice("approved")
        self.assertTrue(stored.message.startswith("Your payment of"))  # Display never rewrites history.
