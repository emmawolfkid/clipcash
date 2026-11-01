import re

from django.contrib.auth import get_user
from django.core import mail
from django.test import override_settings
from django.urls import reverse

from .helpers import NEW_PASSWORD, PASSWORD, AccountsTestCase, User, links_in_last_email, make_user

REGISTER_URL = reverse("accounts:register")
DONE_URL = reverse("accounts:register_done")
LOGIN_URL = reverse("accounts:login")


def activation_link():
    (link,) = [link for link in links_in_last_email() if "/accounts/activate/" in link]
    return link


class RegistrationTests(AccountsTestCase):
    def register(self, email="new@example.com", **extra):
        return self.client.post(REGISTER_URL, {"email": email, **extra})

    def activate(self, link, password=NEW_PASSWORD, client=None):
        client = client or self.client
        response = client.get(link, follow=True)
        set_password_url = response.redirect_chain[-1][0] if response.redirect_chain else link
        return client.post(
            set_password_url, {"new_password1": password, "new_password2": password}
        )

    def test_page_has_no_password_or_privilege_fields(self):
        response = self.client.get(REGISTER_URL)
        self.assertEqual(response.status_code, 200)
        for name in ("password", "password1", "role", "status", "is_staff"):
            self.assertNotContains(response, f'name="{name}"')

    def test_creates_pending_member_without_usable_password(self):
        self.assertRedirects(self.register(email="  New@Example.COM "), DONE_URL)
        user = User.objects.get()
        self.assertEqual(user.email, "new@example.com")
        self.assertEqual((user.role, user.status), (User.Role.MEMBER, User.Status.PENDING))
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.email_verified or user.is_active or user.is_staff or user.is_superuser)
        self.assertEqual(mail.outbox[0].to, ["new@example.com"])
        activation_link()  # Exactly one activation link.

    def test_injected_password_and_privilege_fields_ignored(self):
        self.register(
            password=PASSWORD,
            password1=PASSWORD,
            password2=PASSWORD,
            role=User.Role.SUPER_ADMIN,
            status=User.Status.ACTIVE,
            is_superuser="on",
            email_verified="on",
        )
        user = User.objects.get()
        self.assertFalse(user.has_usable_password())
        self.assertEqual((user.role, user.status), (User.Role.MEMBER, User.Status.PENDING))
        self.assertFalse(user.is_superuser or user.email_verified)

    def test_owner_activates_by_choosing_password(self):
        self.register()
        response = self.activate(activation_link())
        self.assertRedirects(response, reverse("accounts:activation_complete"))

        user = User.objects.get()
        self.assertEqual(user.status, User.Status.ACTIVE)
        self.assertTrue(user.email_verified)
        self.assertTrue(user.check_password(NEW_PASSWORD))
        self.assertFalse(get_user(self.client).is_authenticated)  # No auto-login.

        self.client.post(LOGIN_URL, {"username": "new@example.com", "password": NEW_PASSWORD})
        self.assertTrue(get_user(self.client).is_authenticated)

    def test_token_not_left_in_url_after_first_visit(self):
        self.register()
        response = self.client.get(activation_link())
        self.assertEqual(response.status_code, 302)
        self.assertIn("/set-password/", response["Location"])

    def test_activation_link_single_use(self):
        self.register()
        link = activation_link()
        self.activate(link)
        response = self.client_class().get(link, follow=True)
        self.assertFalse(response.context["validlink"])
        self.assertTrue(User.objects.get().check_password(NEW_PASSWORD))

    def test_weak_password_rejected_and_account_stays_pending(self):
        self.register()
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
        self.register()
        response = self.client.get(activation_link(), follow=True)
        self.assertFalse(response.context["validlink"])

    def test_tampered_link_rejected(self):
        self.register()
        link = re.sub(r"/[^/]+/$", "/bogus-token/", activation_link())
        response = self.client.get(link, follow=True)
        self.assertFalse(response.context["validlink"])

    def test_link_invalid_after_email_change(self):
        self.register()
        link = activation_link()
        user = User.objects.get()
        user.email = "changed@example.com"
        user.save()
        response = self.client.get(link, follow=True)
        self.assertFalse(response.context["validlink"])

    def test_link_invalid_after_account_disabled(self):
        self.register()
        link = activation_link()
        user = User.objects.get()
        user.status = User.Status.DISABLED
        user.save()
        response = self.client.get(link, follow=True)
        self.assertFalse(response.context["validlink"])
        self.assertEqual(User.objects.get().status, User.Status.DISABLED)

    def test_not_logged_in_after_registration(self):
        self.register()
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_invalid_email_rejected(self):
        for email in ("not-an-email", "   ", "a@b", "x" * 250 + "@example.com"):
            with self.subTest(email=email):
                self.assertEqual(self.register(email=email).status_code, 200)
        self.assertFalse(User.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_authenticated_user_redirected(self):
        self.client.force_login(make_user())
        self.assertRedirects(self.client.get(REGISTER_URL), reverse("accounts:profile"))

    def test_rate_limited_per_ip(self):
        for index in range(10):
            self.register(email=f"user{index}@example.com")
        response = self.register(email="blocked@example.com")
        self.assertContains(response, "Too many requests")
        self.assertFalse(User.objects.filter(email="blocked@example.com").exists())


class DuplicateRegistrationTests(AccountsTestCase):
    def register(self, email):
        return self.client.post(REGISTER_URL, {"email": email})

    def test_active_account_owner_notified_with_identical_response(self):
        existing = make_user("taken@example.com")
        self.assertRedirects(self.register("TAKEN@example.com"), DONE_URL)
        self.assertEqual(User.objects.count(), 1)
        existing.refresh_from_db()
        self.assertTrue(existing.check_password(PASSWORD))
        self.assertIn("already has an account", mail.outbox[0].body)
        self.assertNotIn("/accounts/activate/", mail.outbox[0].body)

    def test_pending_account_receives_fresh_activation_link(self):
        self.register("pending@example.com")
        self.register("pending@example.com")
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("/accounts/activate/", mail.outbox[1].body)

    def test_suspended_and_disabled_accounts_get_nothing(self):
        make_user("suspended@example.com", status=User.Status.SUSPENDED)
        make_user("disabled@example.com", status=User.Status.DISABLED)
        for email in ("suspended@example.com", "disabled@example.com"):
            with self.subTest(email=email):
                self.assertRedirects(self.register(email), DONE_URL)
        self.assertEqual(mail.outbox, [])
        self.assertEqual(User.objects.count(), 2)

    def test_per_address_email_limit_gives_identical_response(self):
        for _ in range(5):
            self.assertRedirects(self.register("flood@example.com"), DONE_URL)
        self.assertEqual(len(mail.outbox), 3)


class PasswordPlantingTests(AccountsTestCase):
    """An attacker registering a victim's address can never set or learn its password."""

    def test_attacker_cannot_supply_a_password(self):
        attacker = self.client_class()
        attacker.post(REGISTER_URL, {"email": "victim@example.com", "password1": PASSWORD, "password2": PASSWORD})
        victim = User.objects.get(email="victim@example.com")
        self.assertFalse(victim.has_usable_password())
        attacker.post(LOGIN_URL, {"username": "victim@example.com", "password": PASSWORD})
        self.assertFalse(get_user(attacker).is_authenticated)

    def test_victim_activation_uses_only_victims_password(self):
        attacker = self.client_class()
        attacker.post(REGISTER_URL, {"email": "victim@example.com", "password1": PASSWORD, "password2": PASSWORD})

        # Only the inbox owner receives the link; they choose the password.
        (link,) = [link for link in links_in_last_email() if "/accounts/activate/" in link]
        response = self.client.get(link, follow=True)
        self.client.post(
            response.redirect_chain[-1][0],
            {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
        )

        victim = User.objects.get(email="victim@example.com")
        self.assertEqual(victim.status, User.Status.ACTIVE)
        self.assertTrue(victim.check_password(NEW_PASSWORD))
        self.assertFalse(victim.check_password(PASSWORD))
        attacker.post(LOGIN_URL, {"username": "victim@example.com", "password": PASSWORD})
        self.assertFalse(get_user(attacker).is_authenticated)

    def test_admin_created_pending_password_replaced_on_activation(self):
        # Even a PENDING account that somehow has a password gets the owner's.
        user = User.objects.create_user("p@example.com", PASSWORD)
        self.client.post(REGISTER_URL, {"email": "p@example.com"})
        (link,) = [link for link in links_in_last_email() if "/accounts/activate/" in link]
        response = self.client.get(link, follow=True)
        self.client.post(
            response.redirect_chain[-1][0],
            {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
        )
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW_PASSWORD))
        self.assertFalse(user.check_password(PASSWORD))

    def test_pending_account_cannot_use_password_reset(self):
        self.client.post(REGISTER_URL, {"email": "victim@example.com"})
        mail.outbox.clear()
        self.client.post(reverse("accounts:password_reset"), {"email": "victim@example.com"})
        self.assertEqual(mail.outbox, [])
