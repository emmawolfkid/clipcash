from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

User = get_user_model()

PASSWORD = "Correct-Horse-Battery-9"


class NormalizeEmailTests(TestCase):
    def test_trims_and_lowercases(self):
        self.assertEqual(User.objects.normalize_email("  Alice@Example.COM \t"), "alice@example.com")

    def test_none_becomes_empty_string(self):
        self.assertEqual(User.objects.normalize_email(None), "")


class CreateUserTests(TestCase):
    def test_rejects_missing_or_blank_email(self):
        for email in (None, "", "   ", "\t\n"):
            with self.subTest(email=email), self.assertRaises(ValueError):
                User.objects.create_user(email, PASSWORD)
        self.assertFalse(User.objects.exists())

    def test_rejects_malformed_email(self):
        with self.assertRaises(ValidationError) as ctx:
            User.objects.create_user("not-an-email", PASSWORD)
        self.assertIn("email", ctx.exception.message_dict)

    def test_normalizes_email(self):
        user = User.objects.create_user("  Alice@Example.COM ", PASSWORD)
        self.assertEqual(user.email, "alice@example.com")

    def test_case_insensitive_duplicate_rejected_with_clear_message(self):
        User.objects.create_user("alice@example.com", PASSWORD)
        with self.assertRaises(ValidationError) as ctx:
            User.objects.create_user("ALICE@example.com", PASSWORD)
        self.assertEqual(
            ctx.exception.message_dict,
            {"email": ["A user with this email address already exists."]},
        )
        self.assertEqual(User.objects.count(), 1)

    def test_defaults_are_unprivileged_and_pending(self):
        user = User.objects.create_user("member@example.com", PASSWORD)
        self.assertEqual(user.role, User.Role.MEMBER)
        self.assertEqual(user.status, User.Status.PENDING)
        self.assertFalse(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.email_verified)
        self.assertIsNone(user.phone_number)

    def test_password_is_hashed(self):
        user = User.objects.create_user("member@example.com", PASSWORD)
        self.assertNotEqual(user.password, PASSWORD)
        self.assertTrue(user.check_password(PASSWORD))

    def test_without_password_is_unusable(self):
        user = User.objects.create_user("member@example.com")
        self.assertFalse(user.has_usable_password())

    def test_rejects_super_admin_role(self):
        with self.assertRaises(ValueError):
            User.objects.create_user("x@example.com", PASSWORD, role=User.Role.SUPER_ADMIN)
        self.assertFalse(User.objects.exists())

    def test_rejects_direct_access_flags(self):
        for flag in ("is_staff", "is_superuser", "is_active"):
            for value in (True, False):
                with self.subTest(flag=flag, value=value), self.assertRaises(ValueError):
                    User.objects.create_user("x@example.com", PASSWORD, **{flag: value})
        self.assertFalse(User.objects.exists())

    def test_content_manager_is_staff_but_not_superuser(self):
        user = User.objects.create_user(
            "editor@example.com", PASSWORD, role=User.Role.CONTENT_MANAGER
        )
        self.assertTrue(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_active_status_sets_is_active(self):
        user = User.objects.create_user("m@example.com", PASSWORD, status=User.Status.ACTIVE)
        self.assertTrue(user.is_active)

    def test_rejects_invalid_role_and_status(self):
        for extra in ({"role": "OWNER"}, {"status": "BANNED"}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                User.objects.create_user("x@example.com", PASSWORD, **extra)

    def test_normalizes_phone_number(self):
        user = User.objects.create_user("m@example.com", PASSWORD, phone_number="0712 345 678")
        self.assertEqual(user.phone_number, "+255712345678")

    def test_rejects_invalid_phone_number(self):
        with self.assertRaises(ValidationError) as ctx:
            User.objects.create_user("m@example.com", PASSWORD, phone_number="12345")
        self.assertEqual(list(ctx.exception.message_dict), ["phone_number"])


class CreateSuperuserTests(TestCase):
    def test_creates_consistent_superuser(self):
        user = User.objects.create_superuser("  Root@Example.com", PASSWORD)
        self.assertEqual(user.email, "root@example.com")
        self.assertEqual(user.role, User.Role.SUPER_ADMIN)
        self.assertEqual(user.status, User.Status.ACTIVE)
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)

    def test_accepts_consistent_explicit_flags(self):
        user = User.objects.create_superuser(
            "root@example.com", PASSWORD, is_staff=True, is_superuser=True, is_active=True
        )
        self.assertTrue(user.is_superuser)

    def test_rejects_inconsistent_arguments(self):
        for extra in (
            {"is_staff": False},
            {"is_superuser": False},
            {"is_active": False},
            {"role": User.Role.MEMBER},
            {"role": User.Role.CONTENT_MANAGER},
            {"status": User.Status.PENDING},
            {"status": User.Status.SUSPENDED},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                User.objects.create_superuser("root@example.com", PASSWORD, **extra)
        self.assertFalse(User.objects.exists())

    def test_rejects_blank_email(self):
        with self.assertRaises(ValueError):
            User.objects.create_superuser("  ", PASSWORD)


class NaturalKeyLookupTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            "alice@example.com", PASSWORD, status=User.Status.ACTIVE
        )

    def test_get_by_natural_key_is_case_insensitive(self):
        for email in ("alice@example.com", "ALICE@Example.Com", "  alice@example.com "):
            with self.subTest(email=email):
                self.assertEqual(User.objects.get_by_natural_key(email), self.user)

    def test_get_by_natural_key_missing(self):
        with self.assertRaises(User.DoesNotExist):
            User.objects.get_by_natural_key("bob@example.com")

    def test_authenticate_with_mixed_case_email(self):
        self.assertEqual(authenticate(email="ALICE@example.com", password=PASSWORD), self.user)

    def test_authenticate_rejects_non_active_statuses(self):
        for status in (User.Status.PENDING, User.Status.SUSPENDED, User.Status.DISABLED):
            with self.subTest(status=status):
                self.user.status = status
                self.user.save()
                self.assertIsNone(authenticate(email="alice@example.com", password=PASSWORD))

    def test_authenticate_rejects_wrong_password(self):
        self.assertIsNone(authenticate(email="alice@example.com", password="wrong"))
