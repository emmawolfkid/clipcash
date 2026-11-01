from django.contrib.auth import get_user
from django.contrib.auth.models import Group, Permission
from django.urls import reverse

from .helpers import PASSWORD, AccountsTestCase, User, make_user


def change_url(user):
    return reverse("admin:accounts_user_change", args=[user.pk])


def grant(user, *codenames):
    user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))


class StaffPrivilegeEscalationTests(AccountsTestCase):
    """A Content Manager holding every user permission still cannot escalate."""

    def setUp(self):
        super().setUp()
        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.cm = make_user("cm@example.com", role=User.Role.CONTENT_MANAGER)
        grant(self.cm, "view_user", "add_user", "change_user", "delete_user")
        self.member = make_user("member@example.com")
        self.group = Group.objects.create(name="Everything")
        self.group.permissions.set(Permission.objects.all())
        self.client.force_login(self.cm)

    def post_change(self, user, **overrides):
        data = {
            "email": user.email,
            "first_name": "",
            "last_name": "",
            "phone_number": "",
            "status": user.status,
            "_save": "Save",
        }
        data.update(overrides)
        return self.client.post(change_url(user), data)

    def test_can_manage_members(self):
        self.assertEqual(self.client.get(change_url(self.member)).status_code, 200)
        response = self.post_change(self.member, status=User.Status.SUSPENDED)
        self.assertEqual(response.status_code, 302)
        self.member.refresh_from_db()
        self.assertEqual(self.member.status, User.Status.SUSPENDED)

    def test_cannot_promote_member(self):
        self.post_change(
            self.member,
            role=User.Role.SUPER_ADMIN,
            is_superuser="on",
            is_staff="on",
            groups=[self.group.pk],
            user_permissions=list(Permission.objects.values_list("pk", flat=True)),
            email_verified="on",
        )
        self.member.refresh_from_db()
        self.assertEqual(self.member.role, User.Role.MEMBER)
        self.assertFalse(self.member.is_staff or self.member.is_superuser)
        # Only the automatic role group; the posted "Everything" group was ignored.
        self.assertEqual(list(self.member.groups.values_list("name", flat=True)), ["Members"])
        self.assertFalse(self.member.user_permissions.exists())

    def test_privilege_fields_read_only(self):
        form = self.client.get(change_url(self.member)).context["adminform"].form
        for field in ("role", "groups", "user_permissions", "email_verified", "is_superuser", "is_staff"):
            self.assertNotIn(field, form.fields)

    def test_cannot_change_staff_or_superusers_or_self(self):
        other_cm = make_user("cm2@example.com", role=User.Role.CONTENT_MANAGER)
        for target in (self.root, other_cm, self.cm):
            with self.subTest(target=target.email):
                # With view permission the page renders read-only; saving is refused.
                response = self.client.get(change_url(target))
                self.assertFalse(response.context["has_change_permission"])
                self.assertEqual(self.post_change(target, status=User.Status.SUSPENDED).status_code, 403)
                target.refresh_from_db()
                self.assertEqual(target.status, User.Status.ACTIVE)

    def test_cannot_reset_superuser_password(self):
        # Django names this URL auth_user_password_change even for custom user models.
        url = reverse("admin:auth_user_password_change", args=[self.root.pk])
        response = self.client.post(
            url, {"password1": "Hijacked-Pass-123", "password2": "Hijacked-Pass-123", "usable_password": "true"}
        )
        self.assertEqual(response.status_code, 403)
        self.root.refresh_from_db()
        self.assertTrue(self.root.check_password(PASSWORD))

    def test_cannot_delete_superuser_or_self(self):
        for target in (self.root, self.cm):
            with self.subTest(target=target.email):
                url = reverse("admin:accounts_user_delete", args=[target.pk])
                self.assertEqual(self.client.post(url, {"post": "yes"}).status_code, 403)
                self.assertTrue(User.objects.filter(pk=target.pk).exists())

    def test_added_users_are_always_members(self):
        response = self.client.post(
            reverse("admin:accounts_user_add"),
            {
                "email": "Sneaky@Example.com",
                "phone_number": "",
                "password1": PASSWORD,
                "password2": PASSWORD,
                "usable_password": "true",
                "role": User.Role.SUPER_ADMIN,
                "status": User.Status.ACTIVE,
                "is_superuser": "on",
                "is_staff": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        created = User.objects.get(email="sneaky@example.com")
        self.assertEqual(created.role, User.Role.MEMBER)
        self.assertFalse(created.is_staff or created.is_superuser)

    def test_group_admin_hidden(self):
        grant(self.cm, "view_group", "change_group", "add_group")
        self.assertEqual(self.client.get(reverse("admin:auth_group_changelist")).status_code, 403)
        url = reverse("admin:auth_group_change", args=[self.group.pk])
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_bulk_delete_action_unavailable(self):
        response = self.client.get(reverse("admin:accounts_user_changelist"))
        action_form = response.context["action_form"]
        actions = [] if action_form is None else [c[0] for c in action_form.fields["action"].choices]
        self.assertNotIn("delete_selected", actions)


class SuperuserSafeguardTests(AccountsTestCase):
    def setUp(self):
        super().setUp()
        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.client.force_login(self.root)

    def post_change(self, user, **overrides):
        data = {
            "email": user.email,
            "first_name": "",
            "last_name": "",
            "phone_number": "",
            "role": user.role,
            "status": user.status,
            "_save": "Save",
        }
        if user.email_verified:
            data["email_verified"] = "on"
        data.update(overrides)
        return self.client.post(change_url(user), data)

    def test_superuser_can_promote_member(self):
        member = make_user()
        self.post_change(member, role=User.Role.CONTENT_MANAGER)
        member.refresh_from_db()
        self.assertEqual(member.role, User.Role.CONTENT_MANAGER)
        self.assertTrue(member.is_staff)
        self.assertFalse(member.is_superuser)

    def test_last_super_admin_cannot_demote_or_suspend_self(self):
        for overrides in ({"role": User.Role.MEMBER}, {"status": User.Status.SUSPENDED}):
            with self.subTest(overrides=overrides):
                response = self.post_change(self.root, **overrides)
                self.assertEqual(response.status_code, 200)
                self.assertIn("At least one active Super Administrator must remain.", str(response.content.decode()))
                self.root.refresh_from_db()
                self.assertEqual((self.root.role, self.root.status), (User.Role.SUPER_ADMIN, User.Status.ACTIVE))

    def test_super_admin_can_be_demoted_when_another_remains(self):
        other = make_user("root2@example.com", role=User.Role.SUPER_ADMIN)
        self.post_change(other, role=User.Role.MEMBER)
        other.refresh_from_db()
        self.assertEqual(other.role, User.Role.MEMBER)
        self.assertFalse(other.is_superuser)

    def test_cannot_delete_self_or_last_super_admin(self):
        url = reverse("admin:accounts_user_delete", args=[self.root.pk])
        self.assertEqual(self.client.post(url, {"post": "yes"}).status_code, 403)
        self.assertTrue(User.objects.filter(pk=self.root.pk).exists())


class AdminLoginTests(AccountsTestCase):
    LOGIN_URL = reverse("admin:login")

    def admin_login(self, email, password=PASSWORD):
        return self.client.post(
            self.LOGIN_URL, {"username": email, "password": password, "next": "/admin/"}
        )

    def test_suspended_staff_cannot_sign_in(self):
        make_user("cm@example.com", role=User.Role.CONTENT_MANAGER, status=User.Status.SUSPENDED)
        self.admin_login("cm@example.com")
        self.assertFalse(get_user(self.client).is_authenticated)

    def test_member_cannot_sign_in_to_admin(self):
        make_user()
        self.admin_login("member@example.com")
        self.assertFalse(get_user(self.client).is_authenticated)

    def test_admin_login_throttled(self):
        make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        for _ in range(5):
            self.admin_login("root@example.com", password="wrong-password")
        response = self.admin_login("root@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(get_user(self.client).is_authenticated)

    def test_member_session_cannot_reach_admin(self):
        self.client.force_login(make_user())
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 302)
        self.assertIn(self.LOGIN_URL, response["Location"])


class AdminServerSideValidationTests(AccountsTestCase):
    """Rules hold even for crafted POSTs, not just because fields are hidden."""

    def setUp(self):
        super().setUp()
        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.cm = make_user("cm@example.com", role=User.Role.CONTENT_MANAGER)
        grant(self.cm, "view_user", "add_user", "change_user", "delete_user")

    def post_change(self, client_user, user, **overrides):
        self.client.force_login(client_user)
        data = {"email": user.email, "first_name": "", "last_name": "", "phone_number": "",
                "role": user.role, "status": user.status, "_save": "Save"}
        if user.email_verified:
            data["email_verified"] = "on"
        data.update(overrides)
        return self.client.post(change_url(user), data)

    def test_content_manager_cannot_reactivate_disabled_member(self):
        member = make_user(status=User.Status.DISABLED)
        response = self.post_change(self.cm, member, status=User.Status.ACTIVE)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Only Super Administrators can reactivate disabled accounts.")
        member.refresh_from_db()
        self.assertEqual(member.status, User.Status.DISABLED)

    def test_pending_account_cannot_be_activated_in_admin(self):
        pending = User.objects.create_user("pending@example.com")
        response = self.post_change(self.root, pending, status=User.Status.ACTIVE)
        self.assertEqual(response.status_code, 200)
        pending.refresh_from_db()
        self.assertEqual(pending.status, User.Status.PENDING)

    def test_status_cannot_return_to_pending_in_admin(self):
        member = make_user()
        self.post_change(self.root, member, status=User.Status.PENDING)
        member.refresh_from_db()
        self.assertEqual(member.status, User.Status.ACTIVE)

    def test_content_manager_cannot_create_suspended_or_staff_accounts(self):
        self.client.force_login(self.cm)
        response = self.client.post(reverse("admin:accounts_user_add"), {
            "email": "x@example.com", "phone_number": "", "password1": PASSWORD,
            "password2": PASSWORD, "usable_password": "true", "status": User.Status.SUSPENDED,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="x@example.com").exists())

    def test_admin_changes_are_audited(self):
        from apps.audit.models import AuditEvent

        member = make_user()
        self.post_change(self.root, member, role=User.Role.CONTENT_MANAGER)
        event = AuditEvent.objects.get(action="account.role_changed")
        self.assertEqual((event.actor_id, event.source), (self.root.pk, "ADMIN"))
        self.assertEqual(event.metadata, {"from_role": "MEMBER", "to_role": "CONTENT_MANAGER"})
