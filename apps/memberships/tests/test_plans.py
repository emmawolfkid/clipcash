from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction

from apps.audit.models import AuditEvent
from apps.memberships import services
from apps.memberships.models import MembershipPlan, MembershipPlanVersion

from .helpers import MembershipTestCase, User, make_user


class PlanServiceTests(MembershipTestCase):
    def test_create_records_version_one_and_audit(self):
        self.assertEqual((self.plan.version, self.plan.status, self.plan.currency), (1, "ACTIVE", "TZS"))
        version = self.plan.versions.get()
        self.assertEqual((version.version, version.price_amount, version.duration_days), (1, 5_000, 30))
        event = AuditEvent.objects.get(action="membership.plan.created")
        self.assertEqual(event.metadata, {"code": "monthly", "price_amount": 5_000, "duration_days": 30})

    def test_validation(self):
        cases = {
            "zero price": {"price_amount": 0},
            "negative price": {"price_amount": -1},
            "float price": {"price_amount": 99.5},
            "zero duration": {"duration_days": 0},
            "unknown entitlement": {"entitlements": ["videos.everything"]},
            "too many benefits": {"benefits": [f"Benefit {i}" for i in range(11)]},
            "blank name": {"name": "  "},
        }
        for label, overrides in cases.items():
            values = {"name": "Plan", "price_amount": 1_000, "duration_days": 30, **overrides}
            with self.subTest(label), self.assertRaises(ValidationError):
                services.create_plan(self.root, code=f"bad-{len(label)}", **values)
        with self.assertRaises(ValidationError):  # Duplicate code.
            self.make_plan("monthly")

    def test_term_changes_create_new_version_and_keep_old_snapshots(self):
        pending = services.choose_plan(self.member, self.plan)
        updated = services.update_plan(self.root, self.plan, {"price_amount": 7_500, "duration_days": 60})
        self.assertEqual(updated.version, 2)
        self.assertEqual(list(updated.versions.values_list("version", "price_amount")), [(2, 7_500), (1, 5_000)])
        pending.refresh_from_db()
        self.assertEqual((pending.price_amount, pending.duration_days, pending.plan_version.version), (5_000, 30, 1))

    def test_presentation_changes_do_not_version(self):
        updated = services.update_plan(self.root, self.plan, {"display_order": 5, "description": "Popular"})
        self.assertEqual(updated.version, 1)
        self.assertFalse(AuditEvent.objects.get(action="membership.plan.updated").metadata["new_version"])

    def test_code_and_status_are_not_editable_through_update(self):
        for field in ("code", "status", "version"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                services.update_plan(self.root, self.plan, {field: "x"})

    def test_status_changes(self):
        services.set_plan_status(self.root, self.plan, MembershipPlan.Status.INACTIVE)
        services.set_plan_status(self.root, self.plan, MembershipPlan.Status.ACTIVE)
        services.set_plan_status(self.root, self.plan, MembershipPlan.Status.ARCHIVED)
        with self.assertRaises(services.PlanUnavailable):
            services.set_plan_status(self.root, self.plan, MembershipPlan.Status.ACTIVE)
        with self.assertRaises(services.PlanUnavailable):
            services.update_plan(self.root, self.plan, {"name": "Revived"})
        self.assertEqual(AuditEvent.objects.filter(action="membership.plan.status_changed").count(), 3)

    def test_only_super_admins_manage_plans(self):
        cm = make_user("cm@example.com", role=User.Role.CONTENT_MANAGER)
        for actor in (cm, self.member, None):
            with self.subTest(actor=actor):
                with self.assertRaises(services.NotAuthorized):
                    services.create_plan(actor, code="hack", name="Hack", price_amount=1, duration_days=1)
                with self.assertRaises(services.NotAuthorized):
                    services.update_plan(actor, self.plan, {"price_amount": 1})
                with self.assertRaises(services.NotAuthorized):
                    services.set_plan_status(actor, self.plan, MembershipPlan.Status.ARCHIVED)
        self.plan.refresh_from_db()
        self.assertEqual((self.plan.price_amount, self.plan.status), (5_000, "ACTIVE"))
        self.assertEqual(AuditEvent.objects.filter(action="membership.action.denied").count(), 9)


class PlanDatabaseProtectionTests(MembershipTestCase):
    def assert_sql_rejected(self, sql, params):
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, params)

    def test_plans_and_versions_protected(self):
        self.assert_sql_rejected("UPDATE memberships_membershipplan SET code = 'other' WHERE id = %s", [self.plan.pk])
        self.assert_sql_rejected("DELETE FROM memberships_membershipplan WHERE id = %s", [self.plan.pk])
        self.assert_sql_rejected("UPDATE memberships_membershipplanversion SET price_amount = 1 WHERE plan_id = %s",
                                 [self.plan.pk])
        self.assert_sql_rejected("DELETE FROM memberships_membershipplanversion WHERE plan_id = %s", [self.plan.pk])
        self.assertEqual(MembershipPlanVersion.objects.get(plan=self.plan).price_amount, 5_000)

    def test_archived_plan_cannot_be_reactivated_in_database(self):
        services.set_plan_status(self.root, self.plan, MembershipPlan.Status.ARCHIVED)
        self.assert_sql_rejected("UPDATE memberships_membershipplan SET status = 'ACTIVE' WHERE id = %s", [self.plan.pk])
