"""End-to-end audit coverage of existing account and configuration flows."""

import json

from django.contrib.auth.models import Permission
from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.tests.helpers import NEW_PASSWORD, PASSWORD, AccountsTestCase, User, make_user
from apps.accounts.tokens import password_reset_token_generator
from apps.audit.models import AuditEvent
from apps.core.models import SiteConfiguration

LOGIN_URL = reverse("accounts:login")


def events(action):
    return AuditEvent.objects.filter(action=action)


class AuthenticationAuditTests(AccountsTestCase):
    def test_login_success(self):
        user = make_user()
        self.client.post(LOGIN_URL, {"username": "member@example.com", "password": PASSWORD})
        event = events("auth.login.succeeded").get()
        self.assertEqual((event.actor_id, event.target_id, event.outcome), (user.pk, str(user.pk), "SUCCESS"))
        self.assertEqual(event.metadata, {"interface": "web"})

    def test_login_failure_never_stores_typed_values(self):
        user = make_user()
        typed_password = "My-Secret-Password-9"
        # REG-02: the field takes a mobile number or an email, so a password typed into it
        # is a failed attempt. It is recorded as a keyed hash only, never as typed.
        self.client.post(LOGIN_URL, {"username": typed_password, "password": "x"})
        first = events("auth.login.failed").get()
        self.assertNotIn(typed_password.lower(), json.dumps(
            AuditEvent.objects.filter(pk=first.pk).values().get(), default=str).lower())

        # A password-shaped address and a wrong password: only a keyed hash is kept.
        typed_address = "my-secret-password-9@example.com"
        self.client.post(LOGIN_URL, {"username": typed_address, "password": typed_password})
        self.client.post(LOGIN_URL, {"username": "member@example.com", "password": typed_password})

        failed = list(events("auth.login.failed").order_by("occurred_at"))[1:]
        self.assertEqual(len(failed), 2)
        dump = json.dumps([AuditEvent.objects.filter(pk=e.pk).values().get() for e in failed], default=str)
        self.assertNotIn(typed_password.lower(), dump.lower())
        self.assertNotIn(typed_address, dump)
        self.assertEqual(failed[1].target_id, str(user.pk))
        self.assertTrue(failed[1].metadata["account_exists"])
        self.assertEqual(len(failed[1].metadata["identifier_hash"]), 32)

    def test_unknown_account_failure(self):
        self.client.post(LOGIN_URL, {"username": "nobody@example.com", "password": "x"})
        event = events("auth.login.failed").get()
        self.assertEqual((event.target_id, event.metadata["account_exists"]), ("", False))

    def test_throttled_login_denied(self):
        make_user()
        for _ in range(6):
            self.client.post(LOGIN_URL, {"username": "member@example.com", "password": "wrong"})
        self.assertEqual(events("auth.login.failed").count(), 5)
        self.assertEqual(events("auth.login.throttled").get().outcome, "DENIED")

    def test_admin_login_interface(self):
        make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.client.post(reverse("admin:login"), {"username": "root@example.com", "password": PASSWORD,
                                                  "next": "/admin/"})
        event = events("auth.login.succeeded").get()
        self.assertEqual((event.metadata["interface"], event.source), ("admin", "ADMIN"))

    def test_logout(self):
        user = make_user()
        self.client.force_login(user)
        self.client.post(reverse("accounts:logout"))
        self.assertEqual(events("auth.logout").get().actor_id, user.pk)


class AccountLifecycleAuditTests(AccountsTestCase):
    @override_settings(RESTRICTED_DEMO_MODE=False, PUBLIC_REGISTRATION_APPROVED=True)
    def test_phone_registration_is_recorded_without_the_password(self):
        self.client.post(reverse("accounts:register"), {"phone_number": "0712345678", "password1": NEW_PASSWORD,
                                                        "password2": NEW_PASSWORD})
        user = User.objects.get(phone_number="+255712345678")
        event = events("account.registered").get()
        self.assertEqual((event.actor_id, event.target_id, event.metadata), (user.pk, str(user.pk), {"method": "phone"}))
        self.assertEqual(event.actor_email, "+255712345678")
        self.assertNotIn(NEW_PASSWORD, json.dumps(event.metadata))

    def test_legacy_activation(self):
        user = User.objects.create_user("new@example.com", None, status=User.Status.PENDING)
        self.client.post(reverse("accounts:resend_verification"), {"email": "new@example.com"})

        link = next(word for word in mail.outbox[0].body.split() if "/accounts/activate/" in word)
        response = self.client.get(link, follow=True)
        self.client.post(response.redirect_chain[-1][0],
                         {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
        event = events("account.activated").get()
        self.assertEqual((event.actor_id, event.target_id), (user.pk, str(user.pk)))
        self.assertNotIn(NEW_PASSWORD, json.dumps(event.metadata))

    @override_settings(RESTRICTED_DEMO_MODE=False, PUBLIC_REGISTRATION_APPROVED=True)
    def test_duplicate_registration_is_recorded_once(self):
        User.objects.create_user(None, PASSWORD, phone_number="+255712345678", status=User.Status.ACTIVE)
        self.client.post(reverse("accounts:register"), {"phone_number": "+255 712 345 678",
                                                        "password1": NEW_PASSWORD, "password2": NEW_PASSWORD})
        self.assertEqual(events("account.registration.duplicate").get().metadata, {"method": "phone"})
        self.assertFalse(events("account.registered").exists())

    def test_password_change(self):
        user = make_user()
        self.client.force_login(user)
        self.client.post(reverse("accounts:password_change"), {
            "old_password": PASSWORD, "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD,
        })
        event = events("auth.password.changed").get()
        self.assertEqual((event.actor_id, event.metadata), (user.pk, {}))

    def test_password_reset(self):
        user = make_user()
        self.client.post(reverse("accounts:password_reset"), {"email": "member@example.com"})
        request_event = events("auth.password_reset.requested").get()
        self.assertEqual(request_event.metadata["throttled"], False)

        url = reverse("accounts:password_reset_confirm", kwargs={
            "uidb64": urlsafe_base64_encode(force_bytes(user.pk)),
            "token": password_reset_token_generator.make_token(user),
        })
        response = self.client.get(url, follow=True)
        self.client.post(response.redirect_chain[-1][0],
                         {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
        self.assertEqual(events("auth.password_reset.completed").get().target_id, str(user.pk))


class AdministrativeAuditTests(AccountsTestCase):
    def setUp(self):
        super().setUp()
        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.cm = make_user("cm@example.com", role=User.Role.CONTENT_MANAGER)
        self.cm.user_permissions.add(*Permission.objects.filter(
            codename__in=["view_user", "add_user", "change_user", "delete_user"]))
        self.member = make_user()

    def post_change(self, actor, user, **overrides):
        self.client.force_login(actor)
        data = {"email": user.email, "first_name": "", "last_name": "", "phone_number": "",
                "role": user.role, "status": user.status, "_save": "Save"}
        if user.email_verified:
            data["email_verified"] = "on"
        data.update(overrides)
        return self.client.post(reverse("admin:accounts_user_change", args=[user.pk]), data)

    def test_suspension_and_reactivation(self):
        self.post_change(self.root, self.member, status=User.Status.SUSPENDED)
        self.member.refresh_from_db()
        self.post_change(self.root, self.member, status=User.Status.ACTIVE)
        transitions = [e.metadata for e in events("account.status_changed").order_by("occurred_at")]
        self.assertEqual(transitions, [{"from_status": "ACTIVE", "to_status": "SUSPENDED"},
                                       {"from_status": "SUSPENDED", "to_status": "ACTIVE"}])

    def test_privilege_escalation_attempt_recorded_once(self):
        disabled = make_user("d@example.com", status=User.Status.DISABLED)
        response = self.post_change(self.cm, disabled, status=User.Status.ACTIVE)
        self.assertEqual(response.status_code, 200)  # Form error, no change.
        denial = events("account.access_change.denied").get()
        self.assertEqual((denial.outcome, denial.actor_id, denial.target_id),
                         ("DENIED", self.cm.pk, str(disabled.pk)))
        self.assertIn("reactivation_requires_super_admin", denial.metadata["reasons"])
        self.assertFalse(events("account.status_changed").exists())

    def test_forbidden_admin_write_recorded(self):
        self.client.force_login(self.cm)
        response = self.client.post(reverse("admin:accounts_user_delete", args=[self.root.pk]), {"post": "yes"})
        self.assertEqual(response.status_code, 403)
        event = events("authorization.denied").get()
        self.assertEqual((event.actor_id, event.metadata["method"]), (self.cm.pk, "POST"))
        self.assertEqual(event.metadata["view"], "admin:accounts_user_delete")

    def test_staff_creation_and_deletion(self):
        self.client.force_login(self.root)
        self.client.post(reverse("admin:accounts_user_add"), {
            "email": "new@example.com", "phone_number": "", "password1": PASSWORD, "password2": PASSWORD,
            "usable_password": "true", "role": User.Role.MEMBER, "status": User.Status.ACTIVE,
        })
        created = User.objects.get(email="new@example.com")
        self.assertEqual(events("account.created_by_staff").get().target_id, str(created.pk))

        self.client.post(reverse("admin:accounts_user_delete", args=[created.pk]), {"post": "yes"})
        self.assertFalse(User.objects.filter(pk=created.pk).exists())
        deleted = events("account.deleted").get()
        self.assertEqual((deleted.target_repr, deleted.actor_id), ("new@example.com", self.root.pk))


class ConfigurationAuditTests(AccountsTestCase):
    def test_admin_configuration_change(self):
        root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        cache.clear()
        config = SiteConfiguration.objects.get()
        self.client.force_login(root)
        data = {
            "platform_name": "ClipCash", "platform_description": "", "website_url": "", "company_name": "",
            "company_address": "", "support_email": "", "support_phone": "",
            "default_timezone": config.default_timezone, "default_currency": "TZS", "default_language": "en",
            "maintenance_message": "", "registration_enabled": "on", "maintenance_mode": "on",
            "expected_version": config.version, "_save": "Save",
        }
        self.client.post(reverse("admin:core_siteconfiguration_change", args=[1]), data)
        event = events("config.updated").get()
        self.assertEqual(event.metadata["maintenance_mode"], {"from": False, "to": True})
        self.assertEqual(event.source, "ADMIN")
