"""
Preview or apply the staff permission set defined in apps.accounts.roles.

Only the roles that exist (Super Administrator, Content Manager, Member) are
provisioned, from ROLE_PERMISSIONS. The command:

* previews by default; nothing changes without --apply;
* adds missing role permissions to role groups and puts accounts that lack
  their role group into it (additive, idempotent);
* removes an account from another role's group only with --remove-stale;
* never creates accounts, sets passwords, changes roles, staff or superuser
  flags, or removes permissions from groups;
* fails closed: --apply changes nothing while any role group holds a permission
  outside its approved list (ROLE_PERMISSIONS) or a role permission is missing,
  because adding an account to such a group would grant it unapproved access.
  The check is repeated inside the apply transaction while the group-permission
  table is locked against changes, so it still holds when the transaction commits;
* reports who holds the permissions that separation-of-duties workflows depend on.

    python manage.py provision_staff_permissions            # preview
    python manage.py provision_staff_permissions --apply    # apply additions
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Q

from apps.audit.context import audit_scope
from apps.audit.services import record_business_event

from ...roles import ROLE_GROUP_NAMES, ROLE_PERMISSIONS

# Permissions that two-person (four-eyes) or separated workflows rely on.
SEPARATION_OF_DUTIES = (
    "rewards.manage_reward_policies", "rewards.approve_reward_policies",
    "payments.approve_refunds", "wallets.manage_wallet_restrictions",
    "withdrawals.review_withdrawals", "withdrawals.operate_payout_sandbox",
    "advertising.review_advertisers", "advertising.review_campaigns", "viewing.review_viewing",
)
UNDEFINED_ROLES = ("Finance Officer", "Support Officer", "Auditor")


def _permission(dotted):
    app_label, codename = dotted.split(".")
    return Permission.objects.filter(content_type__app_label=app_label, codename=codename).first()


def _label(permission):
    return f"{permission.content_type.app_label}.{permission.codename}"


class Command(BaseCommand):
    help = "Preview (default) or apply the role groups and permissions defined in apps.accounts.roles."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Make the additions shown in the preview.")
        parser.add_argument("--remove-stale", action="store_true",
                            help="With --apply: also remove accounts from role groups of a role they no longer have.")

    def handle(self, *args, apply, remove_stale, **options):
        self._report(*self._plan(), remove_stale)
        if not apply:
            self.stdout.write("\nPreview only. Run with --apply to make the additions above.")
            return
        with audit_scope(source="CLI"), transaction.atomic():
            for name in ROLE_GROUP_NAMES.values():
                Group.objects.get_or_create(name=name)
            # Group permissions live in the group-permission link table, which Django admin
            # writes without touching the group rows, so locking group rows alone would not
            # stop a concurrent change. SHARE ROW EXCLUSIVE blocks every insert, update and
            # delete there (and other provisioning runs) until this transaction ends, while
            # reads continue. The allowlist is then checked on what is committed now, and
            # it stays true until commit.
            with connection.cursor() as cursor:
                cursor.execute(f"LOCK TABLE {connection.ops.quote_name(Group.permissions.through._meta.db_table)} "
                               "IN SHARE ROW EXCLUSIVE MODE")
            group_additions, missing, extras, user_additions, stale = self._plan()
            if extras or missing:
                raise CommandError(
                    "Refused: nothing was changed. " + "; ".join(
                        [f"{name} holds unapproved permission {_label(p)}" for name, p in extras]
                        + [f"{dotted} does not exist" for dotted in missing])
                    + ". Remove or reassign the unapproved permissions through Django admin (Super Administrator), "
                      "then run the preview again.")
            for role, name, permission in group_additions:
                Group.objects.get(name=name).permissions.add(permission)
            for user, name in user_additions:
                user.groups.add(Group.objects.get(name=name))
            removed = 0
            if remove_stale:
                for user, name in stale:
                    user.groups.remove(Group.objects.get(name=name))
                    removed += 1
            record_business_event("authorization.roles.provisioned", actor=None, metadata={
                "permissions_added": len(group_additions), "group_members_added": len(user_additions),
                "group_members_removed": removed})
        self.stdout.write(self.style.SUCCESS(
            f"\nApplied: {len(group_additions)} group permission(s) added, {len(user_additions)} account(s) "
            f"added to their role group, {removed} stale role-group membership(s) removed."))

    def _plan(self):
        """(group_additions, missing, extras, user_additions, stale) from the current database."""
        User = get_user_model()
        role_group_names = set(ROLE_GROUP_NAMES.values())
        group_additions, missing, extras = [], [], []
        for role, name in ROLE_GROUP_NAMES.items():
            group = Group.objects.filter(name=name).first()
            held = set(group.permissions.select_related("content_type")) if group else set()
            wanted = set()
            for dotted in ROLE_PERMISSIONS[role]:
                permission = _permission(dotted)
                if permission is None:
                    missing.append(dotted)
                else:
                    wanted.add(permission)
            group_additions += [(role, name, p) for p in sorted(wanted - held, key=_label)]
            extras += [(name, p) for p in sorted(held - wanted, key=_label)]

        user_additions, stale = [], []
        for user in User.objects.prefetch_related("groups").order_by("email"):
            names = {g.name for g in user.groups.all()} & role_group_names
            target = ROLE_GROUP_NAMES[user.role]
            if target not in names:
                user_additions.append((user, target))
            stale += [(user, name) for name in sorted(names - {target})]
        return group_additions, missing, extras, user_additions, stale

    def _report(self, group_additions, missing, extras, user_additions, stale, remove_stale):
        out = self.stdout.write
        out("Defined roles: " + ", ".join(f"{role} ({name})" for role, name in ROLE_GROUP_NAMES.items()))
        out("Not defined in this project (no group or permissions are created for them): "
            + ", ".join(UNDEFINED_ROLES) + ". Grant their duties to named staff individually.")
        out("\nRole group permissions to add:")
        for role, name, permission in group_additions:
            out(f"  + {name}: {_label(permission)}")
        if not group_additions:
            out("  none")
        for dotted in missing:
            out(self.style.ERROR(f"  ! {dotted} does not exist (run migrate first)"))
        out("\nAccounts to add to their role group:")
        for user, name in user_additions:
            out(f"  + {user.email} ({user.role}) -> {name}")
        if not user_additions:
            out("  none")
        out("\nAccounts in another role's group" + (" (will be removed):" if remove_stale else
                                                     " (kept; use --remove-stale to remove):"))
        for user, name in stale:
            out(f"  - {user.email} ({user.role}) in {name}")
        if not stale:
            out("  none")
        out("\nUnapproved permissions held by role groups (never removed by this command):")
        for name, permission in extras:
            out(self.style.ERROR(f"  ! SECURITY: {name}: {_label(permission)}"))
        if extras or missing:
            out(self.style.ERROR("  --apply will refuse and change nothing until these are resolved."))
        else:
            out("  none")
        out("\nSeparation-of-duties permissions (active staff holding each, superusers included):")
        User = get_user_model()
        for dotted in SEPARATION_OF_DUTIES:
            permission = _permission(dotted)
            if permission is None:
                continue
            holders = (User.objects.filter(is_active=True, is_staff=True)
                       .filter(Q(is_superuser=True) | Q(user_permissions=permission) | Q(groups__permissions=permission))
                       .distinct().count())
            out(f"  {dotted}: {holders}")
        out("  Two-person workflows need at least two distinct holders; demo withdrawals need a creator, "
            "approver(s) and a different payout initiator.")
