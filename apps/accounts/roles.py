"""Role definitions shared by models, services, migrations and admin."""

# Each role is mirrored by one Django Group so permissions can be granted to the
# group. Super Administrators are Django superusers and need no explicit grants.
ROLE_GROUP_NAMES = {
    "SUPER_ADMIN": "Super Administrators",
    "CONTENT_MANAGER": "Content Managers",
    "MEMBER": "Members",
}

# Model permissions ("app_label.codename") granted to each role's group.
# Least privilege: Content Managers manage video content only; no account,
# membership or financial permissions. Synced after every migrate.
ROLE_PERMISSIONS = {
    "SUPER_ADMIN": (),
    "CONTENT_MANAGER": ("videos.manage_videos", "videos.publish_videos", "videos.manage_categories"),
    "MEMBER": (),
}


def sync_role_groups(**kwargs):
    """post_migrate: ensure role groups exist and hold their role permissions.
    Additive only: permissions a superuser added to a group are kept."""
    from django.contrib.auth.models import Group, Permission

    for role, name in ROLE_GROUP_NAMES.items():
        group, _ = Group.objects.get_or_create(name=name)
        wanted = []
        for dotted in ROLE_PERMISSIONS[role]:
            app_label, codename = dotted.split(".")
            permission = Permission.objects.filter(content_type__app_label=app_label, codename=codename).first()
            if permission is not None:
                wanted.append(permission)
        group.permissions.add(*wanted)


def sync_role_group(user):
    """Put ``user`` in exactly one role group; other (non-role) groups are kept."""
    from django.contrib.auth.models import Group

    target_name = ROLE_GROUP_NAMES[user.role]
    target, _ = Group.objects.get_or_create(name=target_name)
    stale = user.groups.filter(name__in=ROLE_GROUP_NAMES.values()).exclude(pk=target.pk)
    if stale.exists():
        user.groups.remove(*stale)
    user.groups.add(target)
