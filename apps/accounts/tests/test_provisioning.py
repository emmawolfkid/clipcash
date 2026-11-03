"""Phase 14-A: provision_staff_permissions (preview by default, additive, idempotent)."""

from io import StringIO
from unittest import mock

from django.contrib.auth.models import Group, Permission
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from apps.accounts.roles import ROLE_GROUP_NAMES
from apps.audit.models import AuditEvent

from .helpers import User, make_user

CM_GROUP = ROLE_GROUP_NAMES["CONTENT_MANAGER"]
MEMBER_GROUP = ROLE_GROUP_NAMES["MEMBER"]


def perm(dotted):
    app_label, codename = dotted.split(".")
    return Permission.objects.get(content_type__app_label=app_label, codename=codename)


def run(*args):
    out = StringIO()
    call_command("provision_staff_permissions", *args, stdout=out)
    return out.getvalue()


def state():
    return (sorted(Group.objects.values_list("name", "permissions__codename")),
            sorted(User.objects.values_list("email", "groups__name", "is_staff", "is_superuser", "role")),
            User.objects.count())


class ProvisioningTests(TestCase):
    def setUp(self):
        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.cm = make_user("cm@example.com", role=User.Role.CONTENT_MANAGER)
        self.member = make_user("member@example.com")
        self.cm_group = Group.objects.get(name=CM_GROUP)

    def test_preview_changes_nothing(self):
        self.cm_group.permissions.remove(perm("videos.publish_videos"))
        self.cm.groups.clear()
        before = state()
        output = run()
        self.assertIn(f"+ {CM_GROUP}: videos.publish_videos", output)
        self.assertIn(f"+ cm@example.com (CONTENT_MANAGER) -> {CM_GROUP}", output)
        self.assertIn("Preview only", output)
        self.assertEqual(state(), before)
        self.assertFalse(AuditEvent.objects.filter(action="authorization.roles.provisioned").exists())

    def test_apply_restores_role_permissions_and_groups_and_is_idempotent(self):
        self.cm_group.permissions.remove(perm("videos.publish_videos"))
        self.cm.groups.clear()
        run("--apply")
        self.assertTrue(self.cm_group.permissions.filter(codename="publish_videos").exists())
        self.assertTrue(User.objects.get(pk=self.cm.pk).has_perm("videos.publish_videos"))
        event = AuditEvent.objects.get(action="authorization.roles.provisioned")
        self.assertEqual(event.metadata, {"permissions_added": 1, "group_members_added": 1,
                                          "group_members_removed": 0})
        after = state()
        output = run("--apply")
        self.assertEqual(state(), after)
        self.assertIn("Applied: 0 group permission(s) added, 0 account(s)", output)

    def test_never_creates_accounts_or_elevates_anyone(self):
        before_users = list(User.objects.values_list("email", "role", "is_staff", "is_superuser", "status"))
        self.member.groups.clear()
        run("--apply")
        self.assertEqual(list(User.objects.values_list("email", "role", "is_staff", "is_superuser", "status")),
                         before_users)
        member = User.objects.get(pk=self.member.pk)
        self.assertEqual(list(member.groups.values_list("name", flat=True)), [MEMBER_GROUP])
        self.assertEqual(member.get_all_permissions(), set())
        self.assertFalse(Group.objects.get(name=MEMBER_GROUP).permissions.exists())

    def test_unapproved_group_permissions_block_apply_and_are_never_removed(self):
        self.cm_group.permissions.add(perm("payments.view_payments"))
        Group.objects.get(name=MEMBER_GROUP).permissions.add(perm("wallets.view_wallets"))
        preview = run()
        self.assertIn(f"! SECURITY: {CM_GROUP}: payments.view_payments", preview)
        self.assertIn(f"! SECURITY: {MEMBER_GROUP}: wallets.view_wallets", preview)
        self.assertIn("--apply will refuse", preview)
        before = state()
        with self.assertRaises(CommandError) as ctx:
            run("--apply")
        self.assertIn("nothing was changed", str(ctx.exception))
        self.assertEqual(state(), before)
        self.assertTrue(self.cm_group.permissions.filter(codename="view_payments").exists())  # Not removed.
        self.assertFalse(AuditEvent.objects.filter(action="authorization.roles.provisioned").exists())

    def test_elevation_through_a_mutated_group_is_refused(self):
        """R-04: a member outside the Members group must not inherit a permission a
        superuser placed on that group."""
        Group.objects.get(name=MEMBER_GROUP).permissions.add(perm("wallets.view_wallets"))
        self.member.groups.clear()
        with self.assertRaises(CommandError):
            run("--apply")
        member = User.objects.get(pk=self.member.pk)
        self.assertFalse(member.has_perm("wallets.view_wallets"))
        self.assertFalse(member.groups.exists())

    def test_a_refused_apply_leaves_no_partial_changes(self):
        self.cm_group.permissions.remove(perm("videos.publish_videos"))  # Would be added...
        self.cm.groups.clear()  # ...and the account re-added...
        Group.objects.get(name=MEMBER_GROUP).permissions.add(perm("wallets.view_wallets"))  # ...but this blocks.
        before = state()
        with self.assertRaises(CommandError):
            run("--apply")
        self.assertEqual(state(), before)
        self.assertFalse(self.cm_group.permissions.filter(codename="publish_videos").exists())

    def test_group_changed_between_preview_and_apply_is_caught_inside_the_transaction(self):
        from apps.accounts.management.commands import provision_staff_permissions as command

        real_plan = command.Command._plan
        calls = []

        def plan_then_mutate(self_):
            result = real_plan(self_)
            calls.append(1)
            if len(calls) == 1:  # After the preview check, before the locked re-check.
                Group.objects.get(name=CM_GROUP).permissions.add(perm("payments.approve_refunds"))
            return result

        self.cm.groups.clear()
        with mock.patch.object(command.Command, "_plan", plan_then_mutate), self.assertRaises(CommandError):
            run("--apply")
        self.assertFalse(User.objects.get(pk=self.cm.pk).groups.exists())
        self.assertEqual(len(calls), 2)

    def test_clean_apply_keeps_unrelated_permissions_and_memberships(self):
        other_group = Group.objects.create(name="Demo reviewers")
        other_group.permissions.add(perm("viewing.review_viewing"))
        self.cm.groups.add(other_group)
        self.cm.user_permissions.add(perm("payments.view_payments"))
        self.cm_group.permissions.remove(perm("videos.manage_categories"))
        run("--apply")
        cm = User.objects.get(pk=self.cm.pk)
        self.assertEqual(sorted(cm.groups.values_list("name", flat=True)), sorted([CM_GROUP, "Demo reviewers"]))
        self.assertTrue(cm.has_perm("payments.view_payments"))
        self.assertTrue(cm.has_perm("viewing.review_viewing"))
        self.assertTrue(cm.has_perm("videos.manage_categories"))
        self.assertEqual(sorted(other_group.permissions.values_list("codename", flat=True)), ["review_viewing"])

    def test_preview_issues_no_writes(self):
        self.cm_group.permissions.remove(perm("videos.publish_videos"))
        self.member.groups.clear()
        with CaptureQueriesContext(connection) as queries:
            run()
        writes = [q["sql"] for q in queries.captured_queries
                  if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(writes, [])

    def test_stale_role_groups_are_removed_only_when_asked(self):
        self.member.groups.add(self.cm_group)  # A former Content Manager, now a member.
        output = run("--apply")
        self.assertIn(f"- member@example.com (MEMBER) in {CM_GROUP}", output)
        self.assertTrue(self.member.groups.filter(name=CM_GROUP).exists())
        run("--apply", "--remove-stale")
        self.assertFalse(self.member.groups.filter(name=CM_GROUP).exists())
        self.assertEqual(AuditEvent.objects.filter(action="authorization.roles.provisioned")
                         .order_by("-occurred_at").first().metadata["group_members_removed"], 1)

    def test_reports_undefined_roles_and_separation_of_duties_holders(self):
        output = run()
        self.assertIn("Finance Officer, Support Officer, Auditor", output)
        self.assertIn("rewards.approve_reward_policies: 1", output)  # Only the super administrator.
        self.cm.user_permissions.add(perm("rewards.approve_reward_policies"))
        self.assertIn("rewards.approve_reward_policies: 2", run())
