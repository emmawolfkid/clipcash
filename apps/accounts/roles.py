"""Role definitions shared by models, services, migrations and admin."""

# Each role is mirrored by one Django Group so permissions can be granted to the
# group. Super Administrators are Django superusers and need no explicit grants.
ROLE_GROUP_NAMES = {
    "SUPER_ADMIN": "Super Administrators",
    "CONTENT_MANAGER": "Content Managers",
    "MEMBER": "Members",
}

# Model permissions ("app_label.codename") granted to each role's group.
# Least privilege: Content Managers receive content permissions when content
# models exist (Phase 04); they get no account-management permissions.
ROLE_PERMISSIONS = {
    "SUPER_ADMIN": (),
    "CONTENT_MANAGER": (),
    "MEMBER": (),
}


def sync_role_group(user):
    """Put ``user`` in exactly one role group; other (non-role) groups are kept."""
    from django.contrib.auth.models import Group

    target_name = ROLE_GROUP_NAMES[user.role]
    target, _ = Group.objects.get_or_create(name=target_name)
    stale = user.groups.filter(name__in=ROLE_GROUP_NAMES.values()).exclude(pk=target.pk)
    if stale.exists():
        user.groups.remove(*stale)
    user.groups.add(target)
