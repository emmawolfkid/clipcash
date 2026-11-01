from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.test import TestCase, override_settings

from apps.audit import services
from apps.audit.context import audit_scope
from apps.audit.events import UnknownAuditEventError
from apps.audit.models import AuditEvent

User = get_user_model()
PASSWORD = "Correct-Horse-Battery-9"


class ValidationTests(TestCase):
    def test_unknown_action_rejected(self):
        with self.assertRaises(UnknownAuditEventError), transaction.atomic():
            services.record_business_event("account.made_up")

    def test_kind_must_match_registry(self):
        with self.assertRaises(services.AuditError), transaction.atomic():
            services.record_business_event("auth.login.failed")
        with self.assertRaises(services.AuditError):
            services.record_security_event("account.activated", outcome=services.SUCCESS)

    def test_invalid_outcome_rejected(self):
        with self.assertRaises(ValidationError):
            services.record_security_event("auth.logout", outcome="MAYBE")

    def test_metadata_allowlist(self):
        with self.assertRaises(ValidationError), transaction.atomic():
            services.record_business_event("account.role_changed", metadata={"note": "free text"})

    def test_sensitive_keys_rejected_even_nested(self):
        for metadata in (
            {"changes": {"password": "x"}},
            {"changes": {"api_key": {"from": "a", "to": "b"}}},
            {"changes": {"session_cookie": 1}},
            {"changes": {"verification_token": "t"}},
        ):
            with self.subTest(metadata=metadata), self.assertRaises(ValidationError), transaction.atomic():
                services.record_business_event("config.updated", metadata=metadata)

    def test_value_rules(self):
        for metadata in (
            {"version": 1.5},
            {"fields": ["x" * 301]},
            {"fields": list(range(51))},
            {"changes": {"a": {"b": {"c": 1}}}},
            {"changes": {"a": object()}},
        ):
            with self.subTest(metadata=str(metadata)[:40]), self.assertRaises(ValidationError), \
                    transaction.atomic():
                services.record_business_event("config.updated", metadata=metadata)

    def test_valid_metadata_stored(self):
        with transaction.atomic():
            event = services.record_business_event(
                "config.updated", metadata={"version": 2, "fields": ["a"], "changes": {"a": {"from": None, "to": True}}}
            )
        event.refresh_from_db()
        self.assertEqual(event.metadata["changes"], {"a": {"from": None, "to": True}})

    def test_identifier_hash(self):
        first = services.hash_identifier(" Victim@Example.com ")
        self.assertEqual(first, services.hash_identifier("victim@example.com"))
        self.assertEqual(len(first), 32)
        self.assertNotIn("victim", first)
        self.assertEqual(services.hash_identifier(""), "")


class TransactionStrategyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("m@example.com", PASSWORD, status=User.Status.ACTIVE)

    def test_business_event_requires_transaction(self):
        # TestCase wraps each test in a transaction, so simulate "no transaction".
        with mock.patch.object(services.connection, "in_atomic_block", False):
            with self.assertRaises(services.AuditError):
                services.record_business_event("account.activated", target=self.user)

    def test_business_event_rolls_back_with_business_change(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            services.record_business_event("account.activated", target=self.user)
            raise RuntimeError("business failure")
        self.assertFalse(AuditEvent.objects.filter(action="account.activated").exists())

    def test_business_audit_failure_propagates(self):
        with mock.patch.object(AuditEvent, "save", side_effect=DatabaseError("down")):
            with self.assertRaises(DatabaseError), transaction.atomic():
                services.record_business_event("account.activated", target=self.user)

    def test_security_event_survives_rollback_of_rejected_operation(self):
        with audit_scope():
            with self.assertRaises(RuntimeError), transaction.atomic():
                services.record_security_event(
                    "account.access_change.denied", outcome=services.DENIED, target=self.user,
                    metadata={"reasons": ["test"]},
                )
                services.record_business_event("account.activated", target=self.user)
                raise RuntimeError("rejected")
        self.assertTrue(AuditEvent.objects.filter(action="account.access_change.denied").exists())
        self.assertFalse(AuditEvent.objects.filter(action="account.activated").exists())

    def test_security_write_failure_is_logged_not_raised(self):
        with mock.patch.object(AuditEvent, "save", side_effect=DatabaseError("down")):
            with self.assertLogs("clipcash.security", "ERROR") as logs:
                result = services.record_security_event("auth.logout", outcome=services.SUCCESS)
        self.assertIsNone(result)
        self.assertTrue(any("audit_write_failed action=auth.logout" in line for line in logs.output))

    def test_idempotency_key(self):
        with transaction.atomic():
            first = services.record_business_event("audit.purged", metadata={"count": 1}, idempotency_key="k1")
            second = services.record_business_event("audit.purged", metadata={"count": 2}, idempotency_key="k1")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(AuditEvent.objects.filter(idempotency_key="k1").count(), 1)


class SnapshotTests(TestCase):
    def test_actor_and_target_snapshots_survive_deletion(self):
        actor = User.objects.create_superuser("root@example.com", PASSWORD)
        target = User.objects.create_user("gone@example.com", PASSWORD, status=User.Status.ACTIVE)
        with transaction.atomic():
            event = services.record_business_event(
                "account.status_changed", actor=actor, target=target,
                metadata={"from_status": "ACTIVE", "to_status": "DISABLED"},
            )
        target_pk = target.pk
        User.objects.filter(pk=target_pk).delete()

        event = AuditEvent.objects.get(pk=event.pk)
        self.assertEqual((event.actor_id, event.actor_email, event.actor_role),
                         (actor.pk, "root@example.com", "SUPER_ADMIN"))
        self.assertEqual((event.target_app_label, event.target_model, event.target_id, event.target_repr),
                         ("accounts", "user", str(target_pk), "gone@example.com"))

    def test_anonymous_actor(self):
        event = services.record_security_event("auth.login.failed", outcome=services.FAILURE, actor=None)
        self.assertIsNone(event.actor_id)
        self.assertEqual((event.actor_email, event.source), ("", "SYSTEM"))

    def test_scope_sets_source_and_request_id(self):
        with audit_scope(source="CLI", request_id="job-12345678") as scope:
            event = services.record_security_event("auth.logout", outcome=services.SUCCESS, actor=None)
            self.assertIn(event, scope.pending)  # Deferred: TestCase holds a transaction open.
        event = AuditEvent.objects.get(action="auth.logout")
        self.assertEqual((event.source, event.request_id), ("CLI", "job-12345678"))


class RequestContextTests(TestCase):
    def test_request_id_ip_and_user_agent_recorded(self):
        response = self.client.post(
            "/accounts/login/", {"username": "nobody@example.com", "password": "x"},
            HTTP_USER_AGENT="TestBrowser/1.0", HTTP_X_REQUEST_ID="client-chosen-id-123",
        )
        event = AuditEvent.objects.get(action="auth.login.failed")
        self.assertEqual(event.request_id, response["X-Request-ID"])
        self.assertNotEqual(event.request_id, "client-chosen-id-123")  # Client header ignored.
        self.assertEqual((event.ip_address, event.user_agent, event.source), ("127.0.0.1", "TestBrowser/1.0", "WEB"))

    @override_settings(AUDIT_RECORD_IP_ADDRESS=False, AUDIT_RECORD_USER_AGENT=False)
    def test_privacy_controls(self):
        self.client.post("/accounts/login/", {"username": "nobody@example.com", "password": "x"},
                         HTTP_USER_AGENT="TestBrowser/1.0")
        event = AuditEvent.objects.get(action="auth.login.failed")
        self.assertIsNone(event.ip_address)
        self.assertEqual(event.user_agent, "")
