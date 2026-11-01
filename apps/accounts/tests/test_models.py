from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase

User = get_user_model()

PASSWORD = "Correct-Horse-Battery-9"


class EmailNormalizationTests(TestCase):
    def test_objects_create_normalizes_email(self):
        user = User.objects.create(email="  Bob@Example.COM ")
        self.assertEqual(user.email, "bob@example.com")

    def test_save_rejects_blank_email(self):
        for email in ("", "   "):
            with self.subTest(email=email), self.assertRaises(ValueError):
                User(email=email).save()
        self.assertFalse(User.objects.exists())

    def test_full_clean_normalizes_email(self):
        user = User(email=" Carol@Example.com ", password="x")
        user.full_clean()
        self.assertEqual(user.email, "carol@example.com")

    def test_full_clean_reports_case_insensitive_duplicate_once(self):
        User.objects.create_user("carol@example.com", PASSWORD)
        user = User(email="CAROL@example.com", password="x")
        with self.assertRaises(ValidationError) as ctx:
            user.full_clean()
        self.assertEqual(
            ctx.exception.message_dict,
            {"email": ["A user with this email address already exists."]},
        )


class DatabaseConstraintTests(TestCase):
    """Writes that bypass save() are still rejected by PostgreSQL constraints."""

    def setUp(self):
        self.member = User.objects.create_user("member@example.com", PASSWORD)

    def assert_rejected(self, **values):
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=self.member.pk).update(**values)

    def test_case_insensitive_email_uniqueness(self):
        User.objects.create_user("other@example.com", PASSWORD)
        self.assert_rejected(email="OTHER@example.com")

    def test_blank_email(self):
        self.assert_rejected(email="")

    def test_unnormalized_phone_number(self):
        self.assert_rejected(phone_number="0712345678")

    def test_unknown_role_and_status(self):
        self.assert_rejected(role="OWNER")
        self.assert_rejected(status="BANNED")

    def test_flags_inconsistent_with_role(self):
        self.assert_rejected(is_superuser=True, is_staff=True)
        self.assert_rejected(is_staff=True)
        self.assert_rejected(role=User.Role.SUPER_ADMIN)
        self.assert_rejected(role=User.Role.CONTENT_MANAGER)

    def test_active_flag_inconsistent_with_status(self):
        self.assert_rejected(is_active=True)
        self.assert_rejected(status=User.Status.ACTIVE)

    def test_consistent_bulk_update_allowed(self):
        User.objects.filter(pk=self.member.pk).update(status=User.Status.ACTIVE, is_active=True)
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)


class AccessFlagDerivationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("user@example.com", PASSWORD)

    def test_role_change_derives_staff_flags(self):
        self.user.role = User.Role.CONTENT_MANAGER
        self.user.save()
        self.user.refresh_from_db()
        self.assertEqual((self.user.is_staff, self.user.is_superuser), (True, False))

        self.user.role = User.Role.MEMBER
        self.user.save()
        self.user.refresh_from_db()
        self.assertEqual((self.user.is_staff, self.user.is_superuser), (False, False))

    def test_unsaved_instance_defaults_are_unprivileged(self):
        user = User(email="new@example.com")
        self.assertEqual((user.role, user.status), (User.Role.MEMBER, User.Status.PENDING))
        self.assertEqual((user.is_active, user.is_staff, user.is_superuser), (False, False, False))

    def test_access_flags_not_editable_in_model_forms(self):
        from django.forms import modelform_factory

        form_class = modelform_factory(User, fields="__all__")
        for flag in ("is_active", "is_staff", "is_superuser"):
            self.assertNotIn(flag, form_class.base_fields)

    def test_direct_flag_assignment_does_not_grant_privileges(self):
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.is_active = True
        self.user.save()
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_superuser)
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_active)

    def test_status_change_derives_is_active(self):
        for status, expected in (
            (User.Status.ACTIVE, True),
            (User.Status.SUSPENDED, False),
            (User.Status.DISABLED, False),
            (User.Status.PENDING, False),
        ):
            with self.subTest(status=status):
                self.user.status = status
                self.user.save()
                self.user.refresh_from_db()
                self.assertIs(self.user.is_active, expected)

    def test_update_fields_includes_derived_flags(self):
        self.user.role = User.Role.CONTENT_MANAGER
        self.user.status = User.Status.ACTIVE
        self.user.save(update_fields=["role", "status"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff)
        self.assertTrue(self.user.is_active)

    def test_unrelated_update_fields_untouched(self):
        self.user.first_name = "Amani"
        self.user.save(update_fields=["first_name"])
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Amani")

    def test_suspended_superuser_loses_permissions(self):
        root = User.objects.create_superuser("root@example.com", PASSWORD)
        self.assertTrue(root.has_perm("accounts.delete_user"))
        root.status = User.Status.SUSPENDED
        root.save()
        self.assertFalse(root.is_active)
        self.assertFalse(root.has_perm("accounts.delete_user"))


class EmailVerificationIntegrityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("v@example.com", PASSWORD, status=User.Status.ACTIVE)
        self.user.email_verified = True
        self.user.save()
        self.user = User.objects.get(pk=self.user.pk)

    def test_changing_email_clears_verification(self):
        self.user.email = "new@example.com"
        self.user.save()
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)

    def test_changing_email_with_update_fields_clears_verification(self):
        self.user.email = "new@example.com"
        self.user.save(update_fields=["email"])
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)

    def test_case_only_change_keeps_verification(self):
        self.user.email = "V@Example.com"
        self.user.save()
        self.user.refresh_from_db()
        self.assertTrue(self.user.email_verified)

    def test_unrelated_save_keeps_verification(self):
        self.user.first_name = "Neema"
        self.user.save()
        self.user.refresh_from_db()
        self.assertTrue(self.user.email_verified)


class StatusTransitionModelTests(TestCase):
    def test_cannot_return_to_pending(self):
        user = User.objects.create_user("m@example.com", PASSWORD, status=User.Status.ACTIVE)
        user = User.objects.get(pk=user.pk)
        user.status = User.Status.PENDING
        with self.assertRaises(ValidationError) as ctx:
            user.full_clean()
        self.assertIn("status", ctx.exception.message_dict)

    def test_pending_may_stay_pending(self):
        user = User.objects.get(pk=User.objects.create_user("p@example.com").pk)
        user.first_name = "Asha"
        user.full_clean()


class LastSuperAdminTests(TestCase):
    def setUp(self):
        self.root = User.objects.create_superuser("root@example.com", PASSWORD)

    def test_cannot_demote_or_deactivate_last_super_admin(self):
        for field, value in (("role", User.Role.MEMBER), ("status", User.Status.SUSPENDED)):
            with self.subTest(field=field):
                root = User.objects.get(pk=self.root.pk)
                setattr(root, field, value)
                with self.assertRaises(ValidationError) as ctx:
                    root.full_clean()
                self.assertEqual(ctx.exception.error_dict["__all__"][0].code, "last_super_admin")

    def test_can_demote_when_another_active_super_admin_exists(self):
        User.objects.create_superuser("root2@example.com", PASSWORD)
        self.root.role = User.Role.MEMBER
        self.root.full_clean()

    def test_suspended_super_admin_does_not_count(self):
        other = User.objects.create_superuser("root2@example.com", PASSWORD)
        other.status = User.Status.SUSPENDED
        other.save()
        self.root.status = User.Status.DISABLED
        with self.assertRaises(ValidationError):
            self.root.full_clean()


class PhoneNumberModelTests(TestCase):
    def test_optional_and_blank_stored_as_null(self):
        first = User.objects.create_user("a@example.com", PASSWORD, phone_number="")
        second = User.objects.create_user("b@example.com", PASSWORD, phone_number="  ")
        self.assertIsNone(first.phone_number)
        self.assertIsNone(second.phone_number)

    def test_unique_across_formats(self):
        User.objects.create_user("a@example.com", PASSWORD, phone_number="+255712345678")
        with self.assertRaises(ValidationError) as ctx:
            User.objects.create_user("b@example.com", PASSWORD, phone_number="0712 345 678")
        self.assertEqual(
            ctx.exception.message_dict,
            {"phone_number": ["A user with this phone number already exists."]},
        )

    def test_save_normalizes_phone_number(self):
        user = User.objects.create(email="c@example.com", phone_number="255712345678")
        self.assertEqual(user.phone_number, "+255712345678")


class AdminFormTests(TestCase):
    def setUp(self):
        self.root = User.objects.create_superuser("root@example.com", PASSWORD)
        self.model_admin = admin.site._registry[User]
        self.request = RequestFactory().post("/")
        self.request.user = self.root

    def add_form(self, **overrides):
        data = {
            "email": "Dana@Example.com",
            "password1": PASSWORD,
            "password2": PASSWORD,
            "usable_password": "true",
            "role": User.Role.MEMBER,
            "status": User.Status.ACTIVE,
            **overrides,
        }
        return self.model_admin.get_form(self.request, obj=None)(data=data)

    def test_add_form_excludes_derived_flags(self):
        fields = self.model_admin.get_form(self.request, obj=None).base_fields
        for flag in ("is_staff", "is_superuser", "is_active"):
            self.assertNotIn(flag, fields)

    def test_change_form_excludes_derived_flags(self):
        fields = self.model_admin.get_form(self.request, obj=self.root).base_fields
        for flag in ("is_staff", "is_superuser", "is_active"):
            self.assertNotIn(flag, fields)

    def test_add_form_normalizes_email_and_derives_flags(self):
        form = self.add_form(role=User.Role.CONTENT_MANAGER, phone_number="0712345678")
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertEqual(user.email, "dana@example.com")
        self.assertEqual(user.phone_number, "+255712345678")
        self.assertTrue(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.is_active)

    def test_add_form_reports_duplicate_email_once(self):
        User.objects.create_user("dana@example.com", PASSWORD)
        form = self.add_form(email="DANA@example.com")
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors.get("email"), ["A user with this email address already exists."])
        self.assertNotIn("__all__", form.errors)
