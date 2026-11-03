from datetime import timedelta
from unittest import mock

from django.utils import timezone

from apps.audit.context import audit_scope
from apps.audit.models import AuditEvent
from apps.memberships import services
from apps.memberships.models import MembershipPlan, Subscription

from .helpers import MembershipTestCase, User, enable_memberships, make_user, payment_for, payments_enabled

S = Subscription.Status


class ChoosePlanTests(MembershipTestCase):
    def test_creates_pending_with_snapshots(self):
        pending = services.choose_plan(self.member, self.plan)
        self.assertEqual((pending.status, pending.price_amount, pending.duration_days), (S.PENDING, 5_000, 30))
        self.assertEqual((pending.benefits, pending.entitlements), (["Ad-supported library"], ["videos.standard"]))
        self.assertIsNone(pending.starts_at)
        self.assertAlmostEqual((pending.pending_until - timezone.now()).total_seconds(), 7 * 86400, delta=5)
        self.assertEqual(AuditEvent.objects.get(action="membership.subscription.created").metadata,
                         {"plan_code": "monthly", "price_amount": 5_000, "renewal": False})

    def test_repeat_returns_same_pending_and_other_plan_replaces_it(self):
        first = services.choose_plan(self.member, self.plan)
        self.assertEqual(services.choose_plan(self.member, self.plan).pk, first.pk)
        other = services.choose_plan(self.member, self.make_plan("annual", price=50_000, days=365))
        first.refresh_from_db()
        self.assertEqual((first.status, first.cancellation_reason), (S.CANCELLED, "REPLACED"))
        self.assertEqual(Subscription.objects.filter(member=self.member, status=S.PENDING).get().pk, other.pk)

    def test_refused_when_feature_off_account_ineligible_or_plan_unavailable(self):
        enable_memberships(False)
        with audit_scope(), self.assertRaises(services.PlanUnavailable):
            services.choose_plan(self.member, self.plan)
        enable_memberships()
        unverified = make_user("u@example.com", verified=False)
        suspended = make_user("s@example.com", status=User.Status.SUSPENDED)
        for member in (unverified, suspended):
            with self.subTest(member=member.email), self.assertRaises(services.NotEligible):
                services.choose_plan(member, self.plan)
        hidden = self.make_plan("hidden", is_public=False)
        inactive = self.make_plan("inactive")
        services.set_plan_status(self.root, inactive, MembershipPlan.Status.INACTIVE)
        for plan in (hidden, inactive):
            with self.subTest(plan=plan.code), self.assertRaises(services.PlanUnavailable):
                services.choose_plan(self.member, plan)
        self.assertFalse(Subscription.objects.exists())


class PaymentBoundaryTests(MembershipTestCase):
    def setUp(self):
        super().setUp()
        self.pending = services.choose_plan(self.member, self.plan)

    def assert_rejected(self, payment, code):
        with audit_scope(), self.assertRaises(services.MembershipError) as ctx:
            services.activate_with_verified_payment(self.pending.pk, payment)
        self.assertEqual(ctx.exception.code, code)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, S.PENDING)

    def test_locked_off_by_default(self):
        self.assert_rejected(payment_for(self.pending), "payments_disabled")
        self.assertEqual(AuditEvent.objects.get(action="membership.activation.rejected").metadata["reason"],
                         "payments_disabled")

    def test_no_activation_without_verification(self):
        with payments_enabled(verifier_result=False):
            self.assert_rejected(payment_for(self.pending), "payment_not_verified")
        with payments_enabled():
            unknown = payment_for(self.pending)
            object.__setattr__(unknown, "verifier", "made-up")
            self.assert_rejected(unknown, "unknown_verifier")
            self.assert_rejected({"reference": "X", "amount": 5_000}, "payment_not_verified")

    def test_amount_must_match_quoted_price(self):
        services.update_plan(self.root, self.plan, {"price_amount": 9_000})  # Price rises after the choice.
        with payments_enabled():
            self.assert_rejected(payment_for(self.pending, amount=9_000), "amount_mismatch")
            self.assert_rejected(payment_for(self.pending, amount=4_999), "amount_mismatch")
        active = self.activate(self.pending)  # The quoted (snapshot) price is honoured.
        self.assertEqual((active.status, active.amount_paid), (S.ACTIVE, 5_000))

    def test_successful_activation(self):
        active = self.activate(self.pending, reference="PAY-REAL-1")
        self.assertEqual((active.status, active.activation_source, active.payment_reference),
                         (S.ACTIVE, "PAYMENT", "PAY-REAL-1"))
        self.assertAlmostEqual((active.expires_at - active.starts_at).days, 30)
        self.assertEqual(AuditEvent.objects.get(action="membership.subscription.activated").metadata["source"],
                         "PAYMENT")

    def test_duplicate_activation_and_reused_reference_rejected(self):
        self.activate(self.pending, reference="PAY-1")
        with payments_enabled():
            with self.assertRaises(services.InvalidTransition) as ctx:
                services.activate_with_verified_payment(self.pending.pk,
                                                        payment_for(self.pending, reference="PAY-2"))
            self.assertEqual(ctx.exception.code, "not_pending")
            self.pending.refresh_from_db()
            self.assertEqual(self.pending.payment_reference, "PAY-1")  # First activation stands.
            other = services.choose_plan(make_user("other@example.com"), self.plan)
            with self.assertRaises(services.PaymentVerificationError) as ctx:
                services.activate_with_verified_payment(other.pk, payment_for(other, reference="PAY-1"))
        self.assertEqual(ctx.exception.code, "duplicate_reference")

    def test_expired_choice_or_withdrawn_plan_cannot_activate(self):
        services.set_plan_status(self.root, self.plan, MembershipPlan.Status.INACTIVE)
        with payments_enabled(), self.assertRaises(services.PlanUnavailable):
            services.activate_with_verified_payment(self.pending.pk, payment_for(self.pending))
        services.set_plan_status(self.root, self.plan, MembershipPlan.Status.ACTIVE)
        with payments_enabled(), mock.patch("apps.memberships.services.timezone.now",
                                            return_value=timezone.now() + timedelta(days=8)):
            with self.assertRaises(services.InvalidTransition):
                services.activate_with_verified_payment(self.pending.pk, payment_for(self.pending))


class RenewalTests(MembershipTestCase):
    def test_renewal_window_and_queueing(self):
        current = self.active_membership()
        with self.assertRaises(services.RenewalNotAllowed) as ctx:
            services.choose_plan(self.member, self.plan)  # 30 days left > 14-day window.
        self.assertEqual(ctx.exception.code, "too_early")

        later = timezone.now() + timedelta(days=20)
        with mock.patch("apps.memberships.services.timezone.now", return_value=later):
            renewal = services.choose_plan(self.member, self.make_plan("annual", price=50_000, days=365))
            self.assertEqual(renewal.renewal_of_id, current.pk)
            renewal = self.activate(renewal)
            self.assertEqual(renewal.starts_at, current.expires_at)  # Plan change takes effect at period end.
            self.assertTrue(AuditEvent.objects.get(target_id=str(renewal.pk),
                                                   action="membership.subscription.activated").metadata["queued"])
            with self.assertRaises(services.RenewalNotAllowed) as ctx:
                services.choose_plan(self.member, self.plan)
            self.assertEqual(ctx.exception.code, "already_renewed")

    def test_suspended_member_cannot_renew(self):
        services.suspend(self.root, self.active_membership(), "ADMIN_REVIEW")
        with self.assertRaises(services.NotEligible):
            services.choose_plan(self.member, self.plan)


# Complimentary grants: see test_complimentary.py (Phase 14-A).


class AdministrativeLifecycleTests(MembershipTestCase):
    def setUp(self):
        super().setUp()
        self.subscription = self.active_membership()

    def test_suspend_reinstate_cancel(self):
        services.suspend(self.root, self.subscription, "FRAUD_SUSPECTED")
        self.subscription.refresh_from_db()
        self.assertEqual((self.subscription.status, self.subscription.suspension_reason), (S.SUSPENDED, "FRAUD_SUSPECTED"))
        services.reinstate(self.root, self.subscription)
        services.cancel(self.root, self.subscription, "REFUND")
        self.subscription.refresh_from_db()
        self.assertEqual((self.subscription.status, self.subscription.cancellation_reason), (S.CANCELLED, "REFUND"))
        actions = list(AuditEvent.objects.filter(target_id=str(self.subscription.pk))
                       .order_by("occurred_at").values_list("action", flat=True))
        self.assertEqual(actions[-3:], ["membership.subscription.suspended", "membership.subscription.reinstated",
                                        "membership.subscription.cancelled"])

    def test_authorization(self):
        cm = make_user("cm@example.com", role=User.Role.CONTENT_MANAGER)
        for actor in (cm, self.member):
            with self.subTest(actor=actor.email), self.assertRaises(services.NotAuthorized):
                services.suspend(actor, self.subscription, "ADMIN_REVIEW")
        from django.contrib.auth.models import Permission

        cm.user_permissions.add(Permission.objects.get(codename="manage_memberships"))
        cm = User.objects.get(pk=cm.pk)
        services.suspend(cm, self.subscription, "ADMIN_REVIEW")

    def test_invalid_transitions(self):
        with self.assertRaises(services.InvalidTransition):
            services.reinstate(self.root, self.subscription)  # Not suspended.
        services.cancel(self.root, self.subscription, "ADMIN_REVIEW")
        for operation in (lambda: services.suspend(self.root, self.subscription, "ADMIN_REVIEW"),
                          lambda: services.cancel(self.root, self.subscription, "ADMIN_REVIEW")):
            with self.assertRaises(services.InvalidTransition):
                operation()

    def test_reason_required(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            services.suspend(self.root, self.subscription, "because")

    def test_members_cancel_only_their_own_pending_choice(self):
        with self.assertRaises(services.InvalidTransition):
            services.cancel_pending(self.member, self.subscription)  # Active, not pending.
        other = make_user("other@example.com")
        pending = services.choose_plan(other, self.plan)
        with self.assertRaises(services.NotAuthorized):
            services.cancel_pending(self.member, pending)
        services.cancel_pending(other, pending)
        pending.refresh_from_db()
        self.assertEqual((pending.status, pending.cancellation_reason), (S.CANCELLED, "MEMBER_REQUEST"))


class ExpiryTests(MembershipTestCase):
    def test_expiry_and_unpaid_timeout_are_idempotent(self):
        active = self.active_membership()
        other = make_user("other@example.com")
        pending = services.choose_plan(other, self.plan)
        future = timezone.now() + timedelta(days=31)
        self.assertEqual(services.process_expiry(now=future), {"expired": 1, "cancelled": 1})
        self.assertEqual(services.process_expiry(now=future), {"expired": 0, "cancelled": 0})
        active.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual((active.status, pending.status, pending.cancellation_reason),
                         (S.EXPIRED, S.CANCELLED, "UNPAID_TIMEOUT"))
        self.assertEqual(AuditEvent.objects.filter(action="membership.subscription.expired").count(), 1)

    def test_suspended_membership_also_expires_and_cannot_be_reinstated_after(self):
        subscription = self.active_membership()
        services.suspend(self.root, subscription, "ADMIN_REVIEW")
        with mock.patch("apps.memberships.services.timezone.now",
                        return_value=timezone.now() + timedelta(days=31)):
            services.reinstate(self.root, subscription)
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, S.EXPIRED)

    def test_scheduled_job_processes_expiry(self):
        from apps.accounts.tests.helpers import run_worker
        from apps.core.jobs.schedules import SCHEDULES, enqueue_due_schedules

        self.assertIn("memberships.process_expiry", SCHEDULES)
        subscription = self.active_membership()
        with mock.patch("apps.memberships.services.timezone.now",
                        return_value=timezone.now() + timedelta(days=31)):
            enqueue_due_schedules()
            run_worker()
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, S.EXPIRED)
