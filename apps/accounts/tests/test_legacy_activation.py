"""
Accounts created PENDING with an email address (before REG-02, or by staff) still
activate through the emailed link: the owner of the inbox chooses the password, once.
New sign-ups are phone-first (test_registration); this path only finishes old ones.
"""

import re

from django.contrib.auth import get_user
from django.core import mail
from django.test import override_settings
from django.urls import reverse

from .helpers import NEW_PASSWORD, PASSWORD, AccountsTestCase, User, links_in_last_email

RESEND = reverse("accounts:resend_verification")
LOGIN_URL = reverse("accounts:login")


def activation_link():
    (link,) = [link for link in links_in_last_email() if "/accounts/activate/" in link]
    return link


def pending_account(email="new@example.com", password=None):
    return User.objects.create_user(email, password, status=User.Status.PENDING)


class LegacyActivationTests(AccountsTestCase):
    def send_link(self, email="new@example.com"):
        mail.outbox.clear()
        self.assertRedirects(self.client.post(RESEND, {"email": email}), reverse("accounts:resend_verification_done"))

    def activate(self, link, password=NEW_PASSWORD, client=None):
        client = client or self.client
        response = client.get(link, follow=True)
        set_password_url = response.redirect_chain[-1][0] if response.redirect_chain else link
        return client.post(set_password_url, {"new_password1": password, "new_password2": password})

    def setUp(self):
        super().setUp()
        self.user = pending_account()
        self.send_link()

    def test_owner_activates_by_choosing_password(self):
        self.assertRedirects(self.activate(activation_link()), reverse("accounts:activation_complete"))
        user = User.objects.get()
        self.assertEqual((user.status, user.email_verified), (User.Status.ACTIVE, True))
        self.assertTrue(user.check_password(NEW_PASSWORD))
        self.assertFalse(get_user(self.client).is_authenticated)  # No auto-login.
        self.client.post(LOGIN_URL, {"username": "new@example.com", "password": NEW_PASSWORD})
        self.assertTrue(get_user(self.client).is_authenticated)

    def test_token_not_left_in_url_after_first_visit(self):
        response = self.client.get(activation_link())
        self.assertEqual(response.status_code, 302)
        self.assertIn("/set-password/", response["Location"])

    def test_activation_link_single_use(self):
        link = activation_link()
        self.activate(link)
        self.assertFalse(self.client_class().get(link, follow=True).context["validlink"])
        self.assertTrue(User.objects.get().check_password(NEW_PASSWORD))

    def test_weak_password_rejected_and_account_stays_pending(self):
        for weak in ("password123", "short1!", "new@example.com", "83749201384"):
            with self.subTest(weak=weak):
                response = self.activate(activation_link(), password=weak)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors)
        user = User.objects.get()
        self.assertEqual(user.status, User.Status.PENDING)
        self.assertFalse(user.has_usable_password())

    @override_settings(ACCOUNTS_EMAIL_VERIFICATION_TIMEOUT=-1)
    def test_expired_link_rejected(self):
        self.assertFalse(self.client.get(activation_link(), follow=True).context["validlink"])

    def test_tampered_link_rejected(self):
        link = re.sub(r"/[^/]+/$", "/bogus-token/", activation_link())
        self.assertFalse(self.client.get(link, follow=True).context["validlink"])

    def test_link_invalid_after_email_change(self):
        link = activation_link()
        self.user.email = "changed@example.com"
        self.user.save()
        self.assertFalse(self.client.get(link, follow=True).context["validlink"])

    def test_link_invalid_after_account_disabled(self):
        link = activation_link()
        self.user.status = User.Status.DISABLED
        self.user.save()
        self.assertFalse(self.client.get(link, follow=True).context["validlink"])
        self.assertEqual(User.objects.get().status, User.Status.DISABLED)

    def test_pending_account_cannot_use_password_reset(self):
        mail.outbox.clear()
        self.client.post(reverse("accounts:password_reset"), {"email": "new@example.com"})
        self.assertEqual(mail.outbox, [])


class PasswordPlantingTests(AccountsTestCase):
    def test_admin_created_pending_password_replaced_on_activation(self):
        # Even a PENDING account that somehow has a password gets the inbox owner's choice.
        user = pending_account("p@example.com", PASSWORD)
        self.client.post(RESEND, {"email": "p@example.com"})
        response = self.client.get(activation_link(), follow=True)
        self.client.post(response.redirect_chain[-1][0], {"new_password1": NEW_PASSWORD,
                                                          "new_password2": NEW_PASSWORD})
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW_PASSWORD))
        self.assertFalse(user.check_password(PASSWORD))
