import threading
from unittest import skipUnless

from django.contrib.auth.models import AnonymousUser, Group
from django.core.exceptions import ValidationError
from django.db import connection, connections
from django.test import TransactionTestCase

from apps.accounts import services
from apps.accounts.roles import ROLE_GROUP_NAMES
from apps.accounts.tokens import verification_token_generator

from .helpers import NEW_PASSWORD, PASSWORD, AccountsTestCase, User, make_user

Role, Status = User.Role, User.Status


class ChangeAccessTests(AccountsTestCase):
    def setUp(self):
        super().setUp()
        self.root = make_user("root@example.com", role=Role.SUPER_ADMIN)
        self.cm = make_user("cm@example.com", role=Role.CONTENT_MANAGER)
        self.member = make_user()

    def assert_denied(self, actor, user, **change):
        with self.assertRaises(ValidationError):
            services.change_access(actor, user, **change)

    def test_super_admin_can_assign_any_role(self):
        for role in (Role.CONTENT_MANAGER, Role.SUPER_ADMIN, Role.MEMBER):
            with self.subTest(role=role):
                user = services.assign_role(self.root, self.member, role)
                self.assertEqual(user.role, role)
                self.assertEqual(user.is_superuser, role == Role.SUPER_ADMIN)
                self.assertEqual(user.is_staff, role != Role.MEMBER)

    def test_content_manager_cannot_change_roles(self):
        for role in (Role.CONTENT_MANAGER, Role.SUPER_ADMIN):
            with self.subTest(role=role):
                self.assert_denied(self.cm, self.member, role=role)
        self.assert_denied(self.cm, self.cm, role=Role.SUPER_ADMIN)
        self.member.refresh_from_db()
        self.assertEqual(self.member.role, Role.MEMBER)

    def test_content_manager_may_suspend_members_only(self):
        services.change_status(self.cm, self.member, Status.SUSPENDED)
        self.member.refresh_from_db()
        self.assertEqual(self.member.status, Status.SUSPENDED)
        for target in (self.root, self.cm):
            with self.subTest(target=target.email):
                self.assert_denied(self.cm, target, status=Status.SUSPENDED)

    def test_members_and_anonymous_cannot_manage_accounts(self):
        other = make_user("other@example.com")
        for actor in (self.member, AnonymousUser()):
            with self.subTest(actor=actor):
                self.assert_denied(actor, other, status=Status.SUSPENDED)

    def test_inactive_actor_denied(self):
        self.cm.status = Status.SUSPENDED
        self.cm.save()
        self.assert_denied(self.cm, self.member, status=Status.SUSPENDED)

    def test_status_transition_rules(self):
        allowed = [
            (Status.ACTIVE, Status.SUSPENDED),
            (Status.SUSPENDED, Status.ACTIVE),
            (Status.ACTIVE, Status.DISABLED),
            (Status.SUSPENDED, Status.DISABLED),
            (Status.DISABLED, Status.ACTIVE),
        ]
        for old, new in allowed:
            with self.subTest(old=old, new=new):
                User.objects.filter(pk=self.member.pk).update(status=old, is_active=old == Status.ACTIVE)
                self.assertEqual(services.change_status(self.root, self.member, new).status, new)

        for old, new in [(Status.ACTIVE, Status.PENDING), (Status.DISABLED, Status.SUSPENDED),
                         (Status.SUSPENDED, Status.PENDING)]:
            with self.subTest(old=old, new=new):
                User.objects.filter(pk=self.member.pk).update(status=old, is_active=old == Status.ACTIVE)
                self.assert_denied(self.root, self.member, status=new)

    def test_pending_cannot_be_activated_without_verification(self):
        pending = User.objects.create_user("pending@example.com")
        self.assert_denied(self.root, pending, status=Status.ACTIVE)
        services.change_status(self.root, pending, Status.DISABLED)

    def test_only_super_admin_reactivates_disabled(self):
        services.change_status(self.cm, self.member, Status.DISABLED)
        self.assert_denied(self.cm, self.member, status=Status.ACTIVE)
        services.change_status(self.root, self.member, Status.ACTIVE)

    def test_last_super_admin_protected(self):
        for change in ({"role": Role.MEMBER}, {"role": Role.CONTENT_MANAGER},
                       {"status": Status.SUSPENDED}, {"status": Status.DISABLED}):
            with self.subTest(change=change):
                self.assert_denied(self.root, self.root, **change)
        self.root.refresh_from_db()
        self.assertEqual((self.root.role, self.root.status), (Role.SUPER_ADMIN, Status.ACTIVE))

    def test_changes_are_logged_without_email(self):
        with self.assertLogs("clipcash.security", "WARNING") as logs:
            services.change_status(self.root, self.member, Status.SUSPENDED)
            services.assign_role(self.root, self.cm, Role.MEMBER)
        output = "\n".join(logs.output)
        self.assertIn("status_changed", output)
        self.assertIn("role_changed", output)
        self.assertNotIn("@example.com", output)

    def test_role_change_syncs_groups(self):
        services.assign_role(self.root, self.member, Role.CONTENT_MANAGER)
        names = set(self.member.groups.values_list("name", flat=True))
        self.assertEqual(names, {ROLE_GROUP_NAMES["CONTENT_MANAGER"]})


class RoleGroupTests(AccountsTestCase):
    def test_role_groups_created_by_migration(self):
        self.assertEqual(
            set(Group.objects.filter(name__in=ROLE_GROUP_NAMES.values()).values_list("name", flat=True)),
            set(ROLE_GROUP_NAMES.values()),
        )

    def test_new_users_join_their_role_group(self):
        for role in Role.values:
            with self.subTest(role=role):
                user = make_user(f"{role.lower()}@example.com", role=role)
                self.assertEqual(list(user.groups.values_list("name", flat=True)), [ROLE_GROUP_NAMES[role]])

    def test_non_role_groups_preserved(self):
        support = Group.objects.create(name="Support desk")
        user = make_user("cm@example.com", role=Role.CONTENT_MANAGER)
        user.groups.add(support)
        user.role = Role.MEMBER
        user.save()
        self.assertEqual(
            set(user.groups.values_list("name", flat=True)), {"Support desk", ROLE_GROUP_NAMES["MEMBER"]}
        )

    def test_role_groups_carry_no_account_permissions(self):
        for name in ROLE_GROUP_NAMES.values():
            with self.subTest(group=name):
                self.assertFalse(Group.objects.get(name=name).permissions.exists())


class ActivationServiceTests(AccountsTestCase):
    def test_activation_requires_valid_token(self):
        user = User.objects.create_user("p@example.com")
        for token in (None, "", "bogus"):
            with self.subTest(token=token):
                self.assertIsNone(services.activate_account(user.pk, token, NEW_PASSWORD))
        user.refresh_from_db()
        self.assertEqual(user.status, Status.PENDING)

    def test_activation_replay_fails(self):
        user = User.objects.create_user("p@example.com")
        token = verification_token_generator.make_token(user)
        self.assertIsNotNone(services.activate_account(user.pk, token, NEW_PASSWORD))
        self.assertIsNone(services.activate_account(user.pk, token, PASSWORD))
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW_PASSWORD))

    def test_unknown_user(self):
        import uuid

        self.assertIsNone(services.activate_account(uuid.uuid4(), "token", NEW_PASSWORD))


@skipUnless(connection.vendor == "postgresql", "Row locking is verified on PostgreSQL.")
class ConcurrencyTests(TransactionTestCase):
    """Real concurrent transactions against PostgreSQL."""

    def run_concurrently(self, *callables):
        barrier = threading.Barrier(len(callables))
        results = [None] * len(callables)

        def worker(index, func):
            try:
                barrier.wait()
                results[index] = func()
            except Exception as error:  # noqa: BLE001 - collected for assertions
                results[index] = error
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(i, f)) for i, f in enumerate(callables)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results

    def test_activation_token_used_once_under_concurrency(self):
        user = User.objects.create_user("race@example.com")
        token = verification_token_generator.make_token(user)
        results = self.run_concurrently(
            lambda: services.activate_account(user.pk, token, "First-Strong-Phrase-1"),
            lambda: services.activate_account(user.pk, token, "Second-Strong-Phrase-2"),
        )
        self.assertEqual(sum(isinstance(r, User) for r in results), 1, results)
        user.refresh_from_db()
        self.assertEqual(user.status, Status.ACTIVE)

    def test_two_super_admins_cannot_demote_each_other_concurrently(self):
        first = User.objects.create_superuser("one@example.com", PASSWORD)
        second = User.objects.create_superuser("two@example.com", PASSWORD)
        results = self.run_concurrently(
            lambda: services.assign_role(first, second, Role.MEMBER),
            lambda: services.assign_role(second, first, Role.MEMBER),
        )
        remaining = User.objects.filter(role=Role.SUPER_ADMIN, status=Status.ACTIVE).count()
        self.assertEqual(remaining, 1, results)

    def test_concurrent_duplicate_registration_creates_one_account(self):
        from django.test import RequestFactory

        request = RequestFactory().post("/accounts/register/")
        results = self.run_concurrently(
            *[lambda: services.register(email="dup@example.com", request=request)] * 4
        )
        self.assertFalse([r for r in results if isinstance(r, Exception)], results)
        self.assertEqual(User.objects.filter(email="dup@example.com").count(), 1)
