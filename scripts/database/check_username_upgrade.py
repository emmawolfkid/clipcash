"""Rehearse the REG-03 username upgrade (accounts 0005) on a fresh, distinct MariaDB test_* database.

    python scripts/database/check_username_upgrade.py

Refuses an existing test database and never touches the development database. Migrates to the
deployed schema (5e8cc2a: accounts at 0004), creates fictional existing accounts with the
historical model (a Super Administrator, a Content Manager with a group and a direct
permission, a verified email member and a phone-only member), applies accounts 0005, then
checks that every row, group and permission is unchanged, that each account still signs in
with its identifiers in any format, that the new checks hold and that a username account can
be created. Drops only the database it created. Prints PASS lines; exits 1 on any failure.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

BEFORE = ("accounts", "0004_phone_first_identity")
PASSWORD = "Upgrade-Rehearsal-2026"
FAST_HASHER = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def main():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    from django.contrib.auth import authenticate
    from django.contrib.auth.hashers import make_password
    from django.core.management.sql import emit_post_migrate_signal
    from django.db import IntegrityError, connections, transaction
    from django.db.migrations.executor import MigrationExecutor
    from django.test import override_settings

    owner = connections["default"]
    original = owner.settings_dict["NAME"]
    test_name = owner.settings_dict["TEST"]["NAME"]
    assert owner.vendor == "mysql", "MariaDB only"
    assert test_name.startswith("test_") and test_name != original and test_name != "test_clipcash_db"
    checks = []

    def check(condition, label):
        checks.append((bool(condition), label))
        print(("PASS " if condition else "FAIL ") + label)

    created = False
    try:
        with owner.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", [test_name])
            assert cursor.fetchone()[0] == 0, "Refusing to replace an existing database"
            cursor.execute(f"CREATE DATABASE {owner.ops.quote_name(test_name)} "
                           "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            created = True
        owner.close()
        owner.settings_dict["NAME"] = test_name

        executor = MigrationExecutor(owner)
        latest = executor.loader.graph.leaf_nodes()
        before = [BEFORE if app == "accounts" else (app, name) for app, name in latest]
        executor.migrate(before)
        emit_post_migrate_signal(0, False, "default")
        historical = executor.loader.project_state(before).apps
        OldUser = historical.get_model("accounts", "User")
        Group = historical.get_model("auth", "Group")
        Permission = historical.get_model("auth", "Permission")
        check("username" not in {f.name for f in OldUser._meta.get_fields()}, "baseline schema has no username")

        with override_settings(PASSWORD_HASHERS=FAST_HASHER):
            hashed = make_password(PASSWORD)
            rows = {
                "admin": dict(email="upgrade-admin@example.com", role="SUPER_ADMIN", status="ACTIVE", is_staff=True,
                              is_superuser=True, is_active=True, email_verified=True, first_name="Upgrade"),
                "manager": dict(email="upgrade-manager@example.com", role="CONTENT_MANAGER", status="ACTIVE",
                                is_staff=True, is_superuser=False, is_active=True, email_verified=True),
                "email_member": dict(email="upgrade-member@example.com", role="MEMBER", status="ACTIVE",
                                     is_staff=False, is_superuser=False, is_active=True, email_verified=True),
                "phone_member": dict(phone_number="+255712000333", role="MEMBER", status="ACTIVE", is_staff=False,
                                     is_superuser=False, is_active=True, email_verified=False),
            }
            users = {key: OldUser.objects.create(password=hashed, **values) for key, values in rows.items()}
            for key, group_name in (("admin", "Super Administrators"), ("manager", "Content Managers"),
                                    ("email_member", "Members"), ("phone_member", "Members")):
                users[key].groups.add(Group.objects.get_or_create(name=group_name)[0])
            permission = Permission.objects.get(content_type__app_label="payments", codename="verify_manual_payments")
            users["manager"].user_permissions.add(permission)

            def snapshot():
                fields = [f.attname for f in OldUser._meta.concrete_fields]
                with owner.cursor() as cursor:
                    cursor.execute(f"SELECT {', '.join(owner.ops.quote_name(f) for f in fields)} "
                                   "FROM accounts_user ORDER BY id")
                    data = cursor.fetchall()
                    cursor.execute("SELECT user_id, group_id FROM accounts_user_groups ORDER BY 1, 2")
                    groups = cursor.fetchall()
                    cursor.execute("SELECT user_id, permission_id FROM accounts_user_user_permissions ORDER BY 1, 2")
                    permissions = cursor.fetchall()
                return data, groups, permissions

            before_rows = snapshot()

            executor = MigrationExecutor(owner)
            executor.migrate(executor.loader.graph.leaf_nodes())
            applied = MigrationExecutor(owner).loader.applied_migrations
            check(("accounts", "0005_username_sign_in") in applied, "accounts 0005 applied")
            check(snapshot() == before_rows, "every existing row, group and permission is unchanged")

            from apps.accounts.models import User
            from apps.core.mariadb_guards import guard_findings

            check(not User.objects.exclude(username=None).exists(), "existing accounts have no username")
            for key, identifiers in (("admin", ["upgrade-admin@example.com", "UPGRADE-ADMIN@example.com"]),
                                     ("manager", ["upgrade-manager@example.com"]),
                                     ("email_member", ["upgrade-member@example.com"]),
                                     ("phone_member", ["0712 000 333", "+255712000333", "255712000333"])):
                for identifier in identifiers:
                    signed_in = authenticate(None, username=identifier, password=PASSWORD)
                    check(signed_in is not None and signed_in.pk == users[key].pk, f"{key} signs in as {identifier!r}")
            manager = User.objects.get(pk=users["manager"].pk)
            admin = User.objects.get(pk=users["admin"].pk)
            check(manager.is_staff and not manager.is_superuser and manager.has_perm("payments.verify_manual_payments"),
                  "content manager keeps staff status and its permission")
            check(admin.is_staff and admin.is_superuser and admin.role == "SUPER_ADMIN", "super administrator unchanged")

            new = User.objects.create_user(None, PASSWORD, username="Upgrade.User", status=User.Status.ACTIVE)
            check(new.username == "upgrade.user" and authenticate(None, username="UPGRADE.user", password=PASSWORD)
                  == new, "a username account can be created and signs in in any letter case")
            for label, change in (("an account without any identifier", {"email": None, "phone_number": None}),
                                  ("an upper-case username", {"username": "UPGRADE.USER"}),
                                  ("a duplicate username", {"username": "upgrade.user"})):
                target = users["phone_member"].pk if "identifier" in label or "duplicate" in label else new.pk
                try:
                    with transaction.atomic():
                        User.objects.filter(pk=target).update(**change)
                    refused = False
                except IntegrityError:
                    refused = True
                check(refused, f"the database refuses {label}")
            check(guard_findings(owner) == [], "every MariaDB guard trigger is still installed")
    finally:
        owner.close()
        owner.settings_dict["NAME"] = original
        if created:
            with connections["default"].cursor() as cursor:
                cursor.execute(f"DROP DATABASE IF EXISTS {owner.ops.quote_name(test_name)}")
            print(f"dropped {test_name}")
    failed = [label for ok, label in checks if not ok]
    print(f"{len(checks) - len(failed)} of {len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
