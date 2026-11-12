from django.contrib.auth import get_user
from django.urls import reverse

from .helpers import PASSWORD, AccountsTestCase, User, make_user

LOGIN_URL = reverse("accounts:login")
LOGOUT_URL = reverse("accounts:logout")
PROFILE_URL = reverse("accounts:profile")
# REG-01: signing in lands on the member dashboard (its next step is choosing a plan).
DASHBOARD_URL = reverse("dashboard")


class LoginTests(AccountsTestCase):
    def login(self, email="member@example.com", password=PASSWORD, **extra):
        return self.client.post(LOGIN_URL, {"username": email, "password": password, **extra})

    def assert_login_rejected(self, response, code="invalid_login"):
        self.assertEqual(response.status_code, 200)
        self.assertEqual([e.code for e in response.context["form"].non_field_errors().as_data()], [code])
        self.assertFalse(get_user(self.client).is_authenticated)

    def test_active_user_logs_in_case_insensitively(self):
        make_user()
        response = self.login(email="MEMBER@Example.com")
        self.assertRedirects(response, DASHBOARD_URL)
        self.assertTrue(get_user(self.client).is_authenticated)

    def test_session_key_rotated_on_login(self):
        make_user()
        session = self.client.session
        session["pre_login"] = True
        session.save()
        before = session.session_key
        self.login()
        self.assertNotEqual(self.client.session.session_key, before)

    def test_wrong_password_rejected(self):
        make_user()
        self.assert_login_rejected(self.login(password="wrong-password"))

    def test_unknown_email_rejected(self):
        self.assert_login_rejected(self.login(email="nobody@example.com"))

    def test_non_active_statuses_rejected_with_generic_error(self):
        for status in (User.Status.PENDING, User.Status.SUSPENDED, User.Status.DISABLED):
            with self.subTest(status=status):
                make_user(f"{status.lower()}@example.com", status=status)
                self.assert_login_rejected(self.login(email=f"{status.lower()}@example.com"))

    def test_account_locked_after_repeated_failures(self):
        make_user()
        for _ in range(5):
            self.login(password="wrong-password")
        # Even the correct password is refused while throttled.
        self.assert_login_rejected(self.login(), code="throttled")

    def test_success_resets_account_failure_count(self):
        make_user()
        for _ in range(4):
            self.login(password="wrong-password")
        self.assertRedirects(self.login(), DASHBOARD_URL)
        self.client.post(LOGOUT_URL)
        for _ in range(4):
            self.login(password="wrong-password")
        self.assertRedirects(self.login(), DASHBOARD_URL)

    def test_throttle_applies_to_unknown_accounts_too(self):
        for _ in range(5):
            self.login(email="nobody@example.com")
        self.assert_login_rejected(self.login(email="nobody@example.com"), code="throttled")

    def test_ip_locked_after_many_failures_across_accounts(self):
        make_user()
        for index in range(50):
            self.login(email=f"guess{index}@example.com")
        self.assert_login_rejected(self.login(), code="throttled")

    def test_attacker_cannot_lock_out_victim_on_another_network(self):
        make_user()
        attacker = self.client_class(REMOTE_ADDR="203.0.113.9")
        for _ in range(10):
            attacker.post(LOGIN_URL, {"username": "member@example.com", "password": "wrong"})
        victim = self.client_class(REMOTE_ADDR="198.51.100.20")
        response = victim.post(LOGIN_URL, {"username": "member@example.com", "password": PASSWORD})
        self.assertRedirects(response, DASHBOARD_URL)

    def test_distributed_guessing_hits_account_ceiling(self):
        make_user()
        for index in range(50):
            self.client_class(REMOTE_ADDR=f"203.0.113.{index}").post(
                LOGIN_URL, {"username": "member@example.com", "password": "wrong"}
            )
        response = self.client_class(REMOTE_ADDR="198.51.100.20").post(
            LOGIN_URL, {"username": "member@example.com", "password": PASSWORD}
        )
        self.assert_login_rejected(response, code="throttled")

    def test_csrf_enforced(self):
        make_user()
        client = self.client_class(enforce_csrf_checks=True)
        response = client.post(LOGIN_URL, {"username": "member@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, "403_csrf.html")

    def test_external_next_url_ignored(self):
        make_user()
        response = self.login(next="https://evil.example.net/")
        self.assertRedirects(response, DASHBOARD_URL)

    def test_local_next_url_honoured(self):
        make_user()
        response = self.client.post(
            f"{LOGIN_URL}?next={PROFILE_URL}",
            {"username": "member@example.com", "password": PASSWORD, "next": PROFILE_URL},
        )
        self.assertRedirects(response, PROFILE_URL)

    def test_authenticated_user_redirected_from_login(self):
        self.client.force_login(make_user())
        self.assertRedirects(self.client.get(LOGIN_URL), DASHBOARD_URL)


class LogoutTests(AccountsTestCase):
    def test_get_shows_confirmation_and_does_not_log_out(self):
        self.client.force_login(make_user())
        response = self.client.get(LOGOUT_URL)
        self.assertTemplateUsed(response, "accounts/logout_confirm.html")
        self.assertTrue(get_user(self.client).is_authenticated)

    def test_logout_requires_csrf_token(self):
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(make_user())
        self.assertEqual(client.post(LOGOUT_URL).status_code, 403)
        self.assertTrue(get_user(client).is_authenticated)

    def test_post_logs_out(self):
        self.client.force_login(make_user())
        self.assertRedirects(self.client.post(LOGOUT_URL), LOGIN_URL)
        self.assertFalse(get_user(self.client).is_authenticated)


class SessionStatusEnforcementTests(AccountsTestCase):
    def test_existing_session_ends_when_account_suspended_or_disabled(self):
        for status in (User.Status.SUSPENDED, User.Status.DISABLED):
            with self.subTest(status=status):
                user = make_user(f"{status.lower()}@example.com")
                self.client.force_login(user)
                self.assertEqual(self.client.get(PROFILE_URL).status_code, 200)

                user.status = status
                user.save()

                response = self.client.get(PROFILE_URL)
                self.assertRedirects(response, reverse("accounts:account_unavailable"))
                self.assertNotIn("_auth_user_id", self.client.session)  # Session flushed.
                self.assertFalse(get_user(self.client).is_authenticated)

    def test_reactivated_account_must_sign_in_again(self):
        user = make_user()
        self.client.force_login(user)
        user.status = User.Status.SUSPENDED
        user.save()
        self.client.get(PROFILE_URL)
        user.status = User.Status.ACTIVE
        user.save()
        self.assertRedirects(self.client.get(PROFILE_URL), f"{LOGIN_URL}?next={PROFILE_URL}")

    def test_suspended_user_session_cannot_reach_admin(self):
        user = make_user("cm@example.com", role=User.Role.CONTENT_MANAGER)
        self.client.force_login(user)
        user.status = User.Status.SUSPENDED
        user.save()
        response = self.client.get("/admin/")
        self.assertRedirects(response, reverse("accounts:account_unavailable"))

    def test_password_change_ends_other_sessions(self):
        user = make_user()
        self.client.force_login(user)
        user.set_password("Another-Strong-Phrase-42")
        user.save()
        self.assertFalse(get_user(self.client).is_authenticated)
