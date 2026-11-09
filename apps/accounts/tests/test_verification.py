from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.tokens import password_reset_token_generator, verification_token_generator

from .helpers import AccountsTestCase, User, links_in_last_email, make_user

RESEND_URL = reverse("accounts:resend_verification")
RESEND_DONE_URL = reverse("accounts:resend_verification_done")
REQUIRED_URL = reverse("accounts:verification_required")
PROFILE_URL = reverse("accounts:profile")


def confirm_url(user, token=None):
    return reverse(
        "accounts:confirm_email",
        kwargs={
            "uidb64": urlsafe_base64_encode(force_bytes(user.pk)),
            "token": token or verification_token_generator.make_token(user),
        },
    )


class ConfirmEmailTests(AccountsTestCase):
    """Email confirmation for ACTIVE accounts whose address is unverified."""

    def setUp(self):
        super().setUp()
        self.user = make_user("active@example.com", verified=False)

    def test_get_shows_button_without_changing_state(self):
        response = self.client.get(confirm_url(self.user))
        self.assertTemplateUsed(response, "accounts/confirm_email.html")
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)

    def test_post_confirms(self):
        response = self.client.post(confirm_url(self.user))
        self.assertTemplateUsed(response, "accounts/confirm_email_complete.html")
        self.user.refresh_from_db()
        self.assertTrue(self.user.email_verified)

    def test_link_single_use(self):
        url = confirm_url(self.user)
        self.client.post(url)
        self.assertEqual(self.client.post(url).status_code, 400)
        self.assertEqual(self.client.get(url).status_code, 400)

    @override_settings(ACCOUNTS_EMAIL_VERIFICATION_TIMEOUT=-1)
    def test_expired(self):
        self.assertEqual(self.client.post(confirm_url(self.user)).status_code, 400)

    def test_tampered_and_wrong_purpose_tokens_rejected(self):
        for token in ("bogus-token", password_reset_token_generator.make_token(self.user)):
            with self.subTest(token=token):
                self.assertEqual(self.client.post(confirm_url(self.user, token)).status_code, 400)
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)

    def test_invalid_after_email_change(self):
        url = confirm_url(self.user)
        self.user.email = "other@example.com"
        self.user.save()
        self.assertEqual(self.client.post(url).status_code, 400)

    def test_suspended_account_cannot_confirm(self):
        url = confirm_url(self.user)
        self.user.status = User.Status.SUSPENDED
        self.user.save()
        self.assertEqual(self.client.post(url).status_code, 400)
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)

    def test_pending_account_cannot_skip_password_setup(self):
        pending = User.objects.create_user("pending@example.com")
        self.assertEqual(self.client.post(confirm_url(pending)).status_code, 400)
        pending.refresh_from_db()
        self.assertEqual(pending.status, User.Status.PENDING)

    def test_invalid_uid_rejected(self):
        url = reverse("accounts:confirm_email", kwargs={"uidb64": "not-a-uid", "token": "x"})
        self.assertEqual(self.client.get(url).status_code, 400)


class ResendVerificationTests(AccountsTestCase):
    def test_pending_account_gets_activation_link(self):
        User.objects.create_user("pending@example.com")
        self.assertRedirects(self.client.post(RESEND_URL, {"email": "Pending@Example.com"}), RESEND_DONE_URL)
        self.assertTrue(any("/accounts/activate/" in link for link in links_in_last_email()))

    def test_active_unverified_account_gets_confirmation_link(self):
        make_user("active@example.com", verified=False)
        self.client.post(RESEND_URL, {"email": "active@example.com"})
        self.assertTrue(any("/accounts/confirm-email/" in link for link in links_in_last_email()))

    def test_identical_response_without_email_for_other_cases(self):
        make_user("verified@example.com")
        make_user("suspended@example.com", status=User.Status.SUSPENDED, verified=False)
        make_user("disabled@example.com", status=User.Status.DISABLED, verified=False)
        for email in ("nobody@example.com", "verified@example.com", "suspended@example.com", "disabled@example.com"):
            with self.subTest(email=email):
                self.assertRedirects(self.client.post(RESEND_URL, {"email": email}), RESEND_DONE_URL)
        self.assertEqual(mail.outbox, [])

    def test_rate_limited_per_address(self):
        User.objects.create_user("pending@example.com")
        for _ in range(5):
            self.assertRedirects(self.client.post(RESEND_URL, {"email": "pending@example.com"}), RESEND_DONE_URL)
        self.assertEqual(len(mail.outbox), 3)

    def test_signed_in_unverified_user_resends_to_own_address(self):
        self.client.force_login(make_user("active@example.com", verified=False))
        response = self.client.post(RESEND_URL, {"email": "someone-else@example.com"})
        self.assertRedirects(response, REQUIRED_URL)
        self.assertEqual(mail.outbox[0].to, ["active@example.com"])


class VerificationRequiredTests(AccountsTestCase):
    def test_active_unverified_user_redirected_from_member_area(self):
        self.client.force_login(make_user(verified=False))
        self.assertRedirects(self.client.get(PROFILE_URL), REQUIRED_URL)
        self.assertEqual(self.client.get(REQUIRED_URL).status_code, 200)

    def test_verified_user_skips_page(self):
        self.client.force_login(make_user())
        self.assertRedirects(self.client.get(REQUIRED_URL), PROFILE_URL)

    def test_anonymous_redirected_to_login(self):
        self.assertRedirects(
            self.client.get(REQUIRED_URL), f"{reverse('accounts:login')}?next={REQUIRED_URL}"
        )

    def test_email_change_requires_reconfirmation(self):
        user = make_user()
        self.client.force_login(user)
        user.email = "new-address@example.com"
        user.save()
        # RG-03: sessions are bound to the address, so a change made elsewhere (e.g. by staff)
        # ends this session; once signed in again, the new address must be confirmed.
        self.assertRedirects(self.client.get(PROFILE_URL), f"{reverse('accounts:login')}?next={PROFILE_URL}")
        self.client.force_login(user)
        self.assertRedirects(self.client.get(PROFILE_URL), REQUIRED_URL)
