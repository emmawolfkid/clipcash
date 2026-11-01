import threading
from unittest import mock, skipUnless

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase

from apps.core import services
from apps.core.models import SITE_CONFIGURATION_ID, SiteConfiguration

User = get_user_model()
PASSWORD = "Correct-Horse-Battery-9"


def super_admin(email="root@example.com"):
    return User.objects.create_superuser(email, PASSWORD)


def staff(role, email):
    return User.objects.create_user(email, PASSWORD, role=role, status=User.Status.ACTIVE)


class ConfigTestCase(TestCase):
    def setUp(self):
        cache.clear()

    def update(self, actor, **changes):
        version = SiteConfiguration.objects.get().version
        return services.update_site_configuration(actor, expected_version=version, changes=changes)


class DefaultsTests(ConfigTestCase):
    def test_row_created_by_migration_with_safe_defaults(self):
        config = SiteConfiguration.objects.get()
        self.assertEqual(config.pk, SITE_CONFIGURATION_ID)
        self.assertEqual(config.platform_name, "ClipCash")
        self.assertEqual(config.default_currency, "TZS")
        self.assertEqual(config.default_timezone, "Africa/Dar_es_Salaam")
        self.assertEqual(config.default_language, "en")
        self.assertFalse(config.maintenance_mode)
        self.assertTrue(config.registration_enabled)
        self.assertFalse(config.memberships_enabled or config.videos_enabled or config.advertisers_enabled)
        self.assertEqual(config.version, 1)

    def test_no_financial_or_secret_fields_exist(self):
        names = {field.name for field in SiteConfiguration._meta.get_fields()}
        for forbidden in ("secret_key", "database_password", "debug", "allowed_hosts",
                          "real_money_rewards_enabled", "live_payments_enabled", "withdrawals_enabled"):
            self.assertNotIn(forbidden, names)
        self.assertFalse(services.EDITABLE_FIELDS - names)

    def test_missing_row_falls_back_to_defaults_without_writing(self):
        SiteConfiguration.objects.all().delete()
        with self.assertLogs("clipcash.security", "ERROR"):
            config = services.get_site_configuration()
        self.assertTrue(config._state.adding)
        self.assertTrue(config.registration_enabled)
        self.assertFalse(SiteConfiguration.objects.exists())

    def test_database_error_falls_back_to_defaults(self):
        with mock.patch.object(SiteConfiguration.objects, "filter", side_effect=DatabaseError("down")):
            with self.assertLogs("clipcash.security", "ERROR"):
                config = services.get_site_configuration()
        self.assertEqual(config.platform_name, "ClipCash")

    def test_cache_error_does_not_break_reads(self):
        with mock.patch.object(services.cache, "get", side_effect=ConnectionError), \
             mock.patch.object(services.cache, "set", side_effect=ConnectionError):
            self.assertEqual(services.get_site_configuration().pk, SITE_CONFIGURATION_ID)


class DatabaseConstraintTests(ConfigTestCase):
    def assert_rejected(self, **values):
        with self.assertRaises(IntegrityError), transaction.atomic():
            SiteConfiguration.objects.filter(pk=SITE_CONFIGURATION_ID).update(**values)

    def test_second_row_rejected(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            SiteConfiguration.objects.create(id=2)

    def test_unsupported_currency_and_blank_name_rejected(self):
        self.assert_rejected(default_currency="USD")
        self.assert_rejected(platform_name="")


class UpdateServiceTests(ConfigTestCase):
    def setUp(self):
        super().setUp()
        self.root = super_admin()

    def test_super_admin_updates_and_version_increments(self):
        config = self.update(self.root, platform_name="  ClipCash TZ ", support_phone="0712 345 678",
                             maintenance_mode=True)
        self.assertEqual(config.platform_name, "ClipCash TZ")
        self.assertEqual(config.support_phone, "+255712345678")
        self.assertEqual(config.version, 2)
        self.assertEqual(config.updated_by, self.root)

    def test_no_op_update_keeps_version(self):
        self.assertEqual(self.update(self.root, platform_name="ClipCash").version, 1)

    def test_validation(self):
        invalid = {
            "support_email": "not-an-email",
            "support_phone": "12345",
            "website_url": "http://clipcash.example",
            "default_timezone": "Mars/Olympus",
            "default_currency": "USD",
            "default_language": "fr",
            "platform_name": "   ",
            "maintenance_message": "x" * 301,
        }
        for field, value in invalid.items():
            with self.subTest(field=field):
                with self.assertRaises(ValidationError) as ctx:
                    self.update(self.root, **{field: value})
                self.assertIn(field, ctx.exception.message_dict)
        self.assertEqual(SiteConfiguration.objects.get().version, 1)

    def test_valid_regional_values(self):
        config = self.update(self.root, default_timezone="UTC", default_language="sw",
                             website_url="https://clipcash.example")
        self.assertEqual((config.default_timezone, config.default_language), ("UTC", "sw"))

    def test_non_editable_fields_rejected(self):
        for field in ("version", "updated_by", "id", "created_at", "real_money_rewards_enabled",
                      "SECRET_KEY"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.update(self.root, **{field: 1})

    def test_authorization(self):
        cm = staff(User.Role.CONTENT_MANAGER, "cm@example.com")
        member = staff(User.Role.MEMBER, "m@example.com")
        suspended = super_admin("root2@example.com")
        suspended.status = User.Status.SUSPENDED
        suspended.save()
        for actor in (cm, member, suspended, AnonymousUser(), None):
            with self.subTest(actor=actor), self.assertRaises(PermissionError):
                self.update(actor, maintenance_mode=True)
        self.assertFalse(SiteConfiguration.objects.get().maintenance_mode)

    def test_stale_version_rejected(self):
        self.update(self.root, platform_name="First")
        with self.assertRaises(services.StaleConfigurationError):
            services.update_site_configuration(
                self.root, expected_version=1, changes={"platform_name": "Second"}
            )
        self.assertEqual(SiteConfiguration.objects.get().platform_name, "First")

    def test_cache_invalidated_on_update(self):
        self.assertTrue(services.get_site_configuration().registration_enabled)
        self.update(self.root, registration_enabled=False)
        self.assertFalse(services.get_site_configuration().registration_enabled)

    def test_change_audited_once_per_version_and_log_line_omits_values(self):
        from apps.audit.models import AuditEvent

        with self.assertLogs("clipcash.security", "INFO") as logs:
            self.update(self.root, support_email="help@clipcash.example", maintenance_mode=True,
                        videos_enabled=True)
        self.assertNotIn("help@clipcash.example", "\n".join(logs.output))

        event = AuditEvent.objects.get(action="config.updated")
        self.assertEqual(event.idempotency_key, "config.updated:2")
        self.assertEqual(event.metadata["fields"], ["maintenance_mode", "support_email", "videos_enabled"])
        self.assertEqual(event.metadata["feature_flags"], ["videos_enabled"])
        self.assertEqual(event.metadata["maintenance_mode"], {"from": False, "to": True})
        self.assertEqual(event.metadata["changes"]["support_email"], {"from": "", "to": "help@clipcash.example"})

    def test_denied_change_audited(self):
        from apps.audit.context import audit_scope
        from apps.audit.models import AuditEvent

        cm = staff(User.Role.CONTENT_MANAGER, "cm@example.com")
        with audit_scope(), self.assertRaises(PermissionError):
            self.update(cm, maintenance_mode=True)
        event = AuditEvent.objects.get(action="config.update.denied")
        self.assertEqual((event.outcome, event.actor_id), ("DENIED", cm.pk))
        self.assertEqual(event.metadata, {"fields": ["maintenance_mode"], "reason": "not_super_admin"})


@skipUnless(connection.vendor == "postgresql", "Row locking is verified on PostgreSQL.")
class ConcurrentUpdateTests(TransactionTestCase):
    def setUp(self):
        SiteConfiguration.objects.get_or_create(id=SITE_CONFIGURATION_ID)
        cache.clear()

    def test_concurrent_updates_from_same_version_one_wins(self):
        root = super_admin()
        barrier = threading.Barrier(2)
        results = []

        def worker(name):
            try:
                barrier.wait()
                services.update_site_configuration(
                    root, expected_version=1, changes={"platform_name": name}
                )
                results.append("ok")
            except services.StaleConfigurationError:
                results.append("stale")
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(n,)) for n in ("Alpha", "Beta")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(results), ["ok", "stale"])
        config = SiteConfiguration.objects.get()
        self.assertEqual(config.version, 2)
        self.assertIn(config.platform_name, ("Alpha", "Beta"))
