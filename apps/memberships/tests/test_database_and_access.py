import uuid
from datetime import timedelta

from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone

from apps.memberships import services
from apps.memberships.access import check_access
from apps.memberships.models import Subscription

from .helpers import MembershipTestCase, User, enable_memberships, make_user

S = Subscription.Status


class SubscriptionDatabaseProtectionTests(MembershipTestCase):
    def setUp(self):
        super().setUp()
        self.active = self.active_membership()

    def assert_sql_rejected(self, sql, params=None):
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, params or [self.active.pk])

    def test_historical_snapshots_cannot_change(self):
        for column, value in (("price_amount", "1"), ("duration_days", "999"), ("plan_name", "'Cheap'"),
                              ("benefits", "'[]'::jsonb"), ("entitlements", "'[\"videos.premium\"]'::jsonb")):
            with self.subTest(column=column):
                self.assert_sql_rejected(f"UPDATE memberships_subscription SET {column} = {value} WHERE id = %s")
        self.active.refresh_from_db()
        self.assertEqual(self.active.price_amount, 5_000)

    def test_activation_data_frozen_and_state_machine_enforced(self):
        self.assert_sql_rejected("UPDATE memberships_subscription SET expires_at = expires_at + interval '1 year' "
                                 "WHERE id = %s")
        self.assert_sql_rejected("UPDATE memberships_subscription SET payment_reference = 'FORGED' WHERE id = %s")
        self.assert_sql_rejected("UPDATE memberships_subscription SET status = 'PENDING' WHERE id = %s")
        self.assert_sql_rejected("DELETE FROM memberships_subscription WHERE id = %s")
        services.cancel(self.root, self.active, "ADMIN_REVIEW")
        self.assert_sql_rejected("UPDATE memberships_subscription SET status = 'ACTIVE' WHERE id = %s")

    def test_cannot_insert_already_active_or_mark_pending_paid(self):
        pending = services.choose_plan(make_user("other@example.com"), self.plan)
        # Forge "paid" without the payment boundary: amount/reference consistency and the trigger refuse.
        self.assert_sql_rejected(
            "UPDATE memberships_subscription SET status = 'ACTIVE', activated_at = now(), starts_at = now(), "
            "expires_at = now() + interval '30 days', activation_source = 'PAYMENT', amount_paid = 1, "
            "payment_reference = 'FAKE' WHERE id = %s", [pending.pk])
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO memberships_subscription (id, created_at, updated_at, member_id, plan_id, plan_version_id,"
                " plan_name, price_amount, currency, duration_days, benefits, entitlements, status, pending_until,"
                " activation_source, payment_reference, cancellation_reason, suspension_reason, note,"
                " activated_at, starts_at, expires_at, amount_paid)"
                " SELECT %s, now(), now(), member_id, plan_id, plan_version_id, plan_name, price_amount, currency,"
                " duration_days, benefits, entitlements, 'ACTIVE', pending_until, 'COMPLIMENTARY', '', '', '', '',"
                " now(), now(), now() + interval '1 day', 0 FROM memberships_subscription WHERE id = %s",
                [uuid.uuid4(), self.active.pk])

    def test_no_overlapping_periods_even_via_orm(self):
        same_member = Subscription.objects.get(pk=self.active.pk)
        same_member.pk = None
        same_member.id = uuid.uuid4()
        same_member._state.adding = True
        same_member.status, same_member.activated_at, same_member.activation_source = S.PENDING, None, ""
        same_member.payment_reference, same_member.amount_paid = "", None
        same_member.starts_at = same_member.expires_at = None
        same_member.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Subscription.objects.filter(pk=same_member.pk).update(
                status=S.ACTIVE, activated_at=timezone.now(), starts_at=self.active.starts_at,
                expires_at=self.active.expires_at, activation_source="COMPLIMENTARY", amount_paid=0)


class AccessServiceTests(MembershipTestCase):
    def test_reasons(self):
        self.assertEqual(check_access(self.member).reason, "no_membership")
        subscription = self.active_membership()
        self.assertEqual(check_access(self.member).reason, "ok")
        self.assertEqual(check_access(self.member, "videos.standard").reason, "ok")
        self.assertEqual(check_access(self.member, "videos.premium").reason, "entitlement_missing")

        services.suspend(self.root, subscription, "ADMIN_REVIEW")
        self.assertEqual(check_access(self.member).reason, "membership_suspended")
        services.reinstate(self.root, subscription)

        from django.contrib.auth.models import AnonymousUser

        self.assertEqual(check_access(AnonymousUser()).reason, "not_authenticated")
        self.member.email_verified = False
        self.member.save()
        self.assertEqual(check_access(self.member).reason, "email_unverified")
        self.member.email_verified = True
        self.member.status = User.Status.SUSPENDED
        self.member.save()
        self.assertEqual(check_access(self.member).reason, "account_inactive")

    def test_feature_switch(self):
        self.active_membership()
        enable_memberships(False)
        self.assertEqual(check_access(self.member).reason, "feature_disabled")

    def test_ended_period_denied_before_expiry_job_runs(self):
        subscription = self.active_membership()
        after_end = subscription.expires_at + timedelta(seconds=1)
        self.assertEqual(check_access(self.member, now=after_end).reason, "no_membership")
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, S.ACTIVE)  # Job has not run; access is still denied.

    def test_queued_renewal_does_not_grant_access_early(self):
        current = self.active_membership()
        services.cancel(self.root, current, "REFUND")
        later = timezone.now() + timedelta(days=1)
        self.assertEqual(check_access(self.member, now=later).reason, "no_membership")
