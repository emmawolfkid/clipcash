from django.contrib.auth import get_user
from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.tokens import password_reset_token_generator, verification_token_generator

from .helpers import NEW_PASSWORD, PASSWORD, AccountsTestCase, User, links_in_last_email, make_user

RESET_URL = reverse("accounts:password_reset")
RESET_DONE_URL = reverse("accounts:password_reset_done")
COMPLETE_URL = reverse("accounts:password_reset_complete")


def confirm_url(user, generator=password_reset_token_generator):
    return reverse(
        "accounts:password_reset_confirm",
        kwargs={
            "uidb64": urlsafe_base64_encode(force_bytes(user.pk)),
            "token": generator.make_token(user),
        },
    )


class PasswordResetTests(AccountsTestCase):
    def reset_via_link(self, url, password=NEW_PASSWORD, client=None):
        client = client or self.client
        response = client.get(url, follow=True)
        set_password_url = response.redirect_chain[-1][0] if response.redirect_chain else url
        return client.post(set_password_url, {"new_password1": password, "new_password2": password})

    def test_full_reset_flow_ends_all_sessions(self):
        user = make_user()
        other_session = self.client_class()
        other_session.force_login(user)

        self.assertRedirects(self.client.post(RESET_URL, {"email": "MEMBER@example.com"}), RESET_DONE_URL)
        self.assertEqual(mail.outbox[0].to, ["member@example.com"])
        (link,) = [link for link in links_in_last_email() if "/accounts/reset/" in link]

        self.assertRedirects(self.reset_via_link(link), COMPLETE_URL)
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW_PASSWORD))
        self.assertFalse(get_user(self.client).is_authenticated)
        self.assertFalse(get_user(other_session).is_authenticated)

    def test_link_single_use(self):
        url = confirm_url(make_user())
        self.reset_via_link(url)
        self.assertFalse(self.client_class().get(url, follow=True).context["validlink"])

    @override_settings(ACCOUNTS_PASSWORD_RESET_TIMEOUT=-1)
    def test_expired_link_rejected(self):
        url = confirm_url(make_user())
        self.assertFalse(self.client.get(url, follow=True).context["validlink"])

    def test_link_invalid_after_login_or_status_change(self):
        user = make_user()
        url = confirm_url(user)
        user.status = User.Status.SUSPENDED
        user.save()
        self.assertFalse(self.client.get(url, follow=True).context["validlink"])
        user.refresh_from_db()
        self.assertTrue(user.check_password(PASSWORD))

    def test_verification_token_not_accepted_for_reset(self):
        user = make_user()
        url = confirm_url(user, generator=verification_token_generator)
        self.assertFalse(self.client.get(url, follow=True).context["validlink"])

    def test_ineligible_addresses_get_identical_response_and_no_email(self):
        make_user("suspended@example.com", status=User.Status.SUSPENDED)
        make_user("disabled@example.com", status=User.Status.DISABLED)
        User.objects.create_user("pending@example.com")
        unusable = make_user("unusable@example.com")
        unusable.set_unusable_password()
        unusable.save()
        for email in ("nobody@example.com", "suspended@example.com", "disabled@example.com",
                      "pending@example.com", "unusable@example.com"):
            with self.subTest(email=email):
                self.assertRedirects(self.client.post(RESET_URL, {"email": email}), RESET_DONE_URL)
        self.assertEqual(mail.outbox, [])

    def test_disabled_account_cannot_use_old_reset_link(self):
        user = make_user()
        url = confirm_url(user)
        user.status = User.Status.DISABLED
        user.save()
        self.reset_via_link(url)
        user.refresh_from_db()
        self.assertTrue(user.check_password(PASSWORD))
        self.assertEqual(user.status, User.Status.DISABLED)

    def test_weak_new_password_rejected(self):
        user = make_user()
        response = self.reset_via_link(confirm_url(user), password="password123")
        self.assertTrue(response.context["form"].errors)
        user.refresh_from_db()
        self.assertTrue(user.check_password(PASSWORD))

    def test_rate_limited_per_address(self):
        make_user()
        for _ in range(5):
            self.assertRedirects(self.client.post(RESET_URL, {"email": "member@example.com"}), RESET_DONE_URL)
        self.assertEqual(len(mail.outbox), 3)


class PasswordChangeTests(AccountsTestCase):
    URL = reverse("accounts:password_change")

    def test_requires_login(self):
        self.assertRedirects(self.client.get(self.URL), f"{reverse('accounts:login')}?next={self.URL}")

    def test_change_keeps_current_session_and_ends_others(self):
        user = make_user()
        other = self.client_class()
        other.force_login(user)
        self.client.force_login(user)

        response = self.client.post(
            self.URL,
            {"old_password": PASSWORD, "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
        )
        self.assertRedirects(response, reverse("accounts:password_change_done"))
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW_PASSWORD))
        self.assertTrue(get_user(self.client).is_authenticated)
        self.assertFalse(get_user(other).is_authenticated)

    def test_wrong_old_password_rejected(self):
        user = make_user()
        self.client.force_login(user)
        response = self.client.post(
            self.URL,
            {"old_password": "wrong", "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
        )
        self.assertIn("old_password", response.context["form"].errors)
        user.refresh_from_db()
        self.assertTrue(user.check_password(PASSWORD))

    def test_weak_new_password_rejected(self):
        self.client.force_login(make_user())
        response = self.client.post(
            self.URL, {"old_password": PASSWORD, "new_password1": "12345678", "new_password2": "12345678"}
        )
        self.assertTrue(response.context["form"].errors)
