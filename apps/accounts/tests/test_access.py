from django.test import override_settings
from django.urls import reverse

from .helpers import AccountsTestCase, User, make_user


@override_settings(ROOT_URLCONF="apps.accounts.tests.urls")
class AccessControlTests(AccountsTestCase):
    def assert_status(self, url, expected):
        self.assertEqual(self.client.get(url).status_code, expected)

    def test_anonymous_redirected_to_login(self):
        for name in ("probe_member", "probe_staff", "probe_super_admin"):
            with self.subTest(name=name):
                url = reverse(name)
                self.assertRedirects(
                    self.client.get(url), f"{reverse('accounts:login')}?next={url}"
                )

    def test_verified_member(self):
        self.client.force_login(make_user())
        self.assert_status(reverse("probe_member"), 200)
        self.assert_status(reverse("probe_staff"), 403)
        self.assert_status(reverse("probe_super_admin"), 403)

    def test_unverified_member_sent_to_verification(self):
        self.client.force_login(make_user(verified=False))
        self.assertRedirects(
            self.client.get(reverse("probe_member")), reverse("accounts:verification_required")
        )

    def test_content_manager(self):
        self.client.force_login(make_user("cm@example.com", role=User.Role.CONTENT_MANAGER))
        self.assert_status(reverse("probe_member"), 200)
        self.assert_status(reverse("probe_staff"), 200)
        self.assert_status(reverse("probe_super_admin"), 403)

    def test_unverified_content_manager(self):
        self.client.force_login(
            make_user("cm@example.com", role=User.Role.CONTENT_MANAGER, verified=False)
        )
        self.assertRedirects(
            self.client.get(reverse("probe_staff")), reverse("accounts:verification_required")
        )
        self.assert_status(reverse("probe_staff_unverified"), 200)

    def test_super_admin(self):
        self.client.force_login(make_user("root@example.com", role=User.Role.SUPER_ADMIN))
        for name in ("probe_member", "probe_staff", "probe_super_admin"):
            with self.subTest(name=name):
                self.assert_status(reverse(name), 200)
