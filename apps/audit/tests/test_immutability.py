from django.db import DatabaseError, connection, transaction
from django.test import TestCase

from apps.audit import services
from apps.audit.models import AuditEvent, AuditImmutableError


class ImmutabilityTests(TestCase):
    def setUp(self):
        self.event = services.record_security_event("auth.logout", outcome=services.SUCCESS, actor=None)

    def test_orm_modification_blocked(self):
        self.event.outcome = AuditEvent.Outcome.FAILURE
        with self.assertRaises(AuditImmutableError):
            self.event.save()
        with self.assertRaises(AuditImmutableError):
            AuditEvent.objects.filter(pk=self.event.pk).update(outcome="FAILURE")
        with self.assertRaises(AuditImmutableError):
            AuditEvent.objects.bulk_update([self.event], ["outcome"])
        with self.assertRaises(AuditImmutableError):
            AuditEvent.objects.update_or_create(pk=self.event.pk, defaults={"outcome": "FAILURE"})

    def test_orm_deletion_blocked(self):
        with self.assertRaises(AuditImmutableError):
            self.event.delete()
        with self.assertRaises(AuditImmutableError):
            AuditEvent.objects.all().delete()
        self.assertTrue(AuditEvent.objects.filter(pk=self.event.pk).exists())

    def test_raw_sql_update_blocked_by_database(self):
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("UPDATE audit_auditevent SET outcome = 'FAILURE' WHERE id = %s", [self.event.pk])
        self.assertEqual(AuditEvent.objects.get(pk=self.event.pk).outcome, "SUCCESS")

    def test_raw_sql_delete_blocked_by_database(self):
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("DELETE FROM audit_auditevent WHERE id = %s", [self.event.pk])
        self.assertTrue(AuditEvent.objects.filter(pk=self.event.pk).exists())

    def test_purge_flag_allows_delete_but_never_update(self):
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL clipcash.audit_purge = 'on'")
            with self.assertRaises(DatabaseError), transaction.atomic():
                cursor.execute("UPDATE audit_auditevent SET outcome = 'FAILURE'")
            cursor.execute("DELETE FROM audit_auditevent WHERE id = %s", [self.event.pk])
            cursor.execute("SET LOCAL clipcash.audit_purge = 'off'")
        self.assertFalse(AuditEvent.objects.filter(pk=self.event.pk).exists())

    def test_database_constraints(self):
        for values in ({"outcome": "MAYBE"}, {"source": "EMAIL"}, {"category": "MISC"}, {"action": "Bad Action"}):
            fields = {"category": "AUDIT", "action": "audit.purged", "outcome": "SUCCESS",
                      "source": "SYSTEM", **values}
            with self.subTest(values=values), self.assertRaises(DatabaseError), transaction.atomic():
                AuditEvent(**fields).save(force_insert=True)
