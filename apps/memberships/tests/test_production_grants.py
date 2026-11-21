"""PAY-LIVE-01E: complimentary membership grants in production.

The existing grant (services.grant_complimentary, the console review/confirm page and the
COMPLIMENTARY activation source) gains a separate production switch: Super Administrators
only, at most 365 days, a 5-200 character reason, an eligible member without a current,
scheduled or paid-for pending membership, and no payment, ledger or wallet side effect.
The restricted-demo policy is unchanged.
"""

import threading
import uuid
from datetime import timedelta

from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connections, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.memberships import services
from apps.memberships.access import check_access
from apps.memberships.models import Subscription
from apps.notifications.models import Notification
from apps.payments.models import ManualMembershipPayment, PaymentIntent
from apps.payments.tests.manual_helpers import ManualPaymentTestCase
from apps.wallets.models import JournalEntry, Wallet

from .helpers import MembershipTestCase, User, enable_memberships, make_user

PROD = override_settings(RESTRICTED_DEMO_MODE=False, MEMBERSHIPS_PRODUCTION_COMPLIMENTARY_GRANTS_ENABLED=True,
                         MEMBERSHIPS_COMPLIMENTARY_GRANTS_ENABLED=False)
OFF = override_settings(RESTRICTED_DEMO_MODE=False, MEMBERSHIPS_PRODUCTION_COMPLIMENTARY_GRANTS_ENABLED=False,
                        MEMBERSHIPS_COMPLIMENTARY_GRANTS_ENABLED=False)
DEMO = override_settings(RESTRICTED_DEMO_MODE=True, MEMBERSHIPS_COMPLIMENTARY_GRANTS_ENABLED=True,
                         MEMBERSHIPS_PRODUCTION_COMPLIMENTARY_GRANTS_ENABLED=False)
S = Subscription.Status
REASON = "Partner launch access"


def op():
    return uuid.uuid4().hex


def money_records():
    return {model.__name__: model.objects.count()
            for model in (JournalEntry, ManualMembershipPayment, PaymentIntent, Wallet)}


def staff(email, *permissions):
    user = make_user(email, role=User.Role.CONTENT_MANAGER)
    for dotted in permissions:
        app_label, codename = dotted.split(".")
        user.user_permissions.add(Permission.objects.get(content_type__app_label=app_label, codename=codename))
    return User.objects.get(pk=user.pk)


class GrantMixin:
    def grant(self, member=None, plan=None, days=60, reason=REASON, actor=None, operation=None, expires_at=None):
        return services.grant_complimentary(
            actor or self.root, member or self.member, plan or self.plan,
            expires_at=expires_at or timezone.now() + timedelta(days=days), reason=reason,
            operation_id=operation or op())


# --- Switches ---------------------------------------------------------------------------------


class GrantModeTests(GrantMixin, MembershipTestCase):
    @OFF
    def test_off_by_default_in_production(self):
        self.assertIsNone(services.complimentary_grant_mode())
        with self.assertRaises(services.NotAuthorized) as caught:
            self.grant()
        self.assertEqual(caught.exception.code, "grants_disabled")
        self.client.force_login(self.root)
        self.assertEqual(self.client.get(reverse("administration:member_complimentary",
                                                 args=[self.member.pk])).status_code, 403)
        self.assertFalse(Subscription.objects.filter(activation_source="COMPLIMENTARY").exists())

    @PROD
    def test_production_switch_enables_production_grants(self):
        self.assertEqual(services.complimentary_grant_mode(), services.GRANT_MODE_PRODUCTION)
        self.assertEqual(self.grant().activation_source, "COMPLIMENTARY")

    @override_settings(RESTRICTED_DEMO_MODE=False, MEMBERSHIPS_COMPLIMENTARY_GRANTS_ENABLED=True,
                       MEMBERSHIPS_PRODUCTION_COMPLIMENTARY_GRANTS_ENABLED=False)
    def test_the_demo_switch_never_enables_production_grants(self):
        self.assertIsNone(services.complimentary_grant_mode())

    @override_settings(RESTRICTED_DEMO_MODE=True, MEMBERSHIPS_COMPLIMENTARY_GRANTS_ENABLED=False,
                       MEMBERSHIPS_PRODUCTION_COMPLIMENTARY_GRANTS_ENABLED=True)
    def test_the_production_switch_never_enables_demo_grants(self):
        self.assertIsNone(services.complimentary_grant_mode())

    @DEMO
    def test_demo_policy_is_unchanged(self):
        self.assertEqual(services.complimentary_grant_mode(), services.GRANT_MODE_DEMO)
        with self.assertRaises(ValidationError):  # Demo: never beyond the plan's own 30 days.
            self.grant(days=45)
        granted = self.grant(days=20)
        self.assertEqual(AuditEvent.objects.get(action=services.GRANT_ACTION).metadata["mode"], "demo")
        note = Notification.objects.get(recipient=self.member, kind="membership.complimentary")
        self.assertIn("demonstration access", note.message)
        self.assertEqual(granted.amount_paid, 0)


# --- Production grants --------------------------------------------------------------------------


@PROD
class ProductionGrantTests(GrantMixin, MembershipTestCase):
    def test_grant_is_a_labelled_non_payment_membership_with_frozen_terms(self):
        before = money_records()
        granted = self.grant(days=200)  # Longer than the plan's 30 days: allowed in production.
        self.assertEqual((granted.status, granted.activation_source, granted.amount_paid, granted.payment_reference,
                          granted.note), (S.ACTIVE, "COMPLIMENTARY", 0, "", REASON))
        self.assertEqual(granted.plan_version.version, self.plan.version)
        self.assertEqual(granted.entitlements, ["videos.standard"])
        self.assertAlmostEqual((granted.expires_at - granted.starts_at).total_seconds(), 200 * 86400, delta=5)
        self.assertEqual(money_records(), before)
        # Later plan changes never alter the granted terms.
        services.update_plan(self.root, self.plan, {"entitlements": ["videos.standard", "videos.premium"],
                                                    "price_amount": 9_000})
        granted.refresh_from_db()
        self.assertEqual((granted.entitlements, granted.price_amount), (["videos.standard"], 5_000))

    def test_grant_is_audited_and_notified(self):
        granted = self.grant(days=90)
        event = AuditEvent.objects.get(action=services.GRANT_ACTION)
        self.assertEqual((event.actor_id, event.target_id), (self.root.pk, str(granted.pk)))
        for key, value in {"member": str(self.member.pk), "plan_code": "monthly", "reason": REASON,
                           "mode": "production", "amount_paid": 0, "plan_version": self.plan.version}.items():
            self.assertEqual(event.metadata[key], value, key)
        self.assertAlmostEqual(event.metadata["duration_minutes"], 90 * 24 * 60, delta=1)
        self.assertEqual(event.metadata["expires_at"], granted.expires_at.isoformat())
        note = Notification.objects.get(recipient=self.member, kind="membership.complimentary")
        self.assertIn("complimentary access", note.message)
        self.assertNotIn("demonstration", note.message)

    def test_entitlement_and_expiry_checks_still_apply(self):
        granted = self.grant(days=10)
        self.assertEqual(check_access(self.member, "videos.standard").reason, "ok")
        self.assertEqual(check_access(self.member, "videos.premium").reason, "entitlement_missing")
        self.assertEqual(check_access(self.member, now=granted.expires_at + timedelta(seconds=1)).reason,
                         "no_membership")

    def test_duration_is_bounded_to_365_days(self):
        self.grant(days=365, expires_at=timezone.now() + timedelta(days=365) - timedelta(minutes=1))
        other = make_user("other@example.com")
        for expires_at in (timezone.now() + timedelta(days=366), timezone.now() - timedelta(minutes=1)):
            with self.subTest(expires_at=expires_at), self.assertRaises(ValidationError):
                self.grant(member=other, expires_at=expires_at)
        with self.assertRaises(ValidationError):  # A naive date and time is refused.
            self.grant(member=other, expires_at=(timezone.now() + timedelta(days=5)).replace(tzinfo=None))

    def test_reason_must_be_5_to_200_characters(self):
        for reason in ("", "abcd", "x" * 201):
            with self.subTest(length=len(reason)), self.assertRaises(ValidationError):
                self.grant(reason=reason)
        self.grant(reason="x" * 200)

    def test_only_super_administrators_grant(self):
        manager = staff("ops@example.com", "memberships.manage_memberships", "accounts.view_user")
        for actor in (manager, self.member):
            with self.subTest(actor=actor.email), self.assertRaises(services.NotAuthorized):
                self.grant(actor=actor)
        self.assertTrue(AuditEvent.objects.filter(action="membership.action.denied").exists())
        self.assertFalse(Subscription.objects.filter(activation_source="COMPLIMENTARY").exists())

    def test_no_self_grant(self):
        with self.assertRaises(services.NotAuthorized) as caught:
            self.grant(member=self.root)
        self.assertEqual(caught.exception.code, "own_membership")

    def test_ineligible_accounts_are_refused(self):
        unverified = make_user("unverified@example.com", verified=False)
        suspended = make_user("suspended@example.com")
        User.objects.filter(pk=suspended.pk).update(status=User.Status.SUSPENDED, is_active=False)
        suspended.refresh_from_db()
        not_member = staff("staffer@example.com")
        for member in (unverified, suspended, not_member):
            with self.subTest(member=member.email), self.assertRaises(services.NotEligible):
                self.grant(member=member)

    def test_inactive_plan_is_refused(self):
        services.set_plan_status(self.root, self.plan, "INACTIVE")
        with self.assertRaises(services.PlanUnavailable):
            self.grant()

    def test_current_or_scheduled_memberships_are_never_overlapped(self):
        self.active_membership()
        with self.assertRaises(services.NotEligible) as caught:
            self.grant()
        self.assertEqual(caught.exception.code, "membership_exists")
        self.assertFalse(Subscription.objects.filter(activation_source="COMPLIMENTARY").exists())

    def test_a_pending_choice_of_another_plan_is_refused_and_kept(self):
        annual = self.make_plan("annual", days=365)
        pending = services.choose_plan(self.member, annual)
        with self.assertRaises(services.InvalidTransition) as caught:
            self.grant()
        self.assertEqual(caught.exception.code, "pending_choice_conflict")
        pending.refresh_from_db()
        self.assertEqual(pending.status, S.PENDING)

    def test_repeated_submission_returns_the_original_grant(self):
        operation = op()
        expires_at = timezone.now() + timedelta(days=30)
        first = self.grant(operation=operation, expires_at=expires_at)
        again = self.grant(operation=operation, expires_at=expires_at)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(Subscription.objects.filter(activation_source="COMPLIMENTARY").count(), 1)
        with self.assertRaises(services.InvalidTransition):  # Same token, different request.
            self.grant(operation=operation, expires_at=expires_at, reason="A different reason")

    def test_database_guards_still_protect_the_grant(self):
        granted = self.grant()
        for change in ({"amount_paid": 5_000}, {"expires_at": granted.expires_at + timedelta(days=1)},
                       {"payment_reference": "AIRTEL-LIPA:FAKE123"}):
            with self.subTest(change=change), self.assertRaises(DatabaseError), transaction.atomic():
                Subscription.objects.filter(pk=granted.pk).update(**change)


@override_settings(MEMBERSHIPS_PRODUCTION_COMPLIMENTARY_GRANTS_ENABLED=True,
                   MEMBERSHIPS_COMPLIMENTARY_GRANTS_ENABLED=False)
class PaidChoiceProtectionTests(GrantMixin, ManualPaymentTestCase):
    """A member who may have paid is never switched to a free membership."""

    def test_choice_with_a_payment_awaiting_verification_is_refused(self):
        claim = self.submit()
        with self.assertRaises(services.InvalidTransition) as caught:
            self.grant()
        self.assertEqual(caught.exception.code, "payment_under_review")
        claim.refresh_from_db()
        self.assertEqual((claim.status, claim.subscription.status), ("PENDING_VERIFICATION", S.PENDING))


# --- Operations Console and member pages -------------------------------------------------------


@PROD
class ConsoleAndMemberPageTests(GrantMixin, MembershipTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("administration:member_complimentary", args=[self.member.pk])

    def data(self, step, days=45, **extra):
        expires = timezone.localtime(timezone.now() + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
        return {"step": step, "plan": str(self.plan.pk), "expires_at": expires, "reason": REASON,
                "operation": self.operation, **extra}

    def test_review_confirm_and_see_the_result(self):
        self.client.force_login(self.root)
        detail = self.client.get(reverse("administration:member", args=[self.member.pk]))
        self.assertContains(detail, "Grant complimentary membership")
        self.assertNotContains(detail, "Grant complimentary demo membership")
        page = self.client.get(self.url)
        self.assertContains(page, "at most 365 days")
        self.assertNotContains(page, "DJANGO_")
        self.operation = page.context["form"].initial["operation"]
        review = self.client.post(self.url, self.data("review"))
        self.assertContains(review, "Review and confirm")
        self.assertContains(review, f"version {self.plan.version}, kept as granted")
        self.assertContains(review, "no payment, ledger or wallet record is created")
        self.assertFalse(Subscription.objects.filter(activation_source="COMPLIMENTARY").exists())  # Review only.
        unconfirmed = self.client.post(self.url, self.data("confirm"))
        self.assertEqual(unconfirmed.status_code, 400)
        done = self.client.post(self.url, self.data("confirm", confirm="on"))
        self.assertRedirects(done, reverse("administration:member", args=[self.member.pk]))
        again = self.client.post(self.url, self.data("confirm", confirm="on"))  # Double submit.
        self.assertEqual(again.status_code, 302)
        self.assertEqual(Subscription.objects.filter(activation_source="COMPLIMENTARY").count(), 1)
        result = self.client.get(reverse("administration:member", args=[self.member.pk]))
        self.assertContains(result, "Complimentary grant")
        self.assertContains(result, "membership.complimentary.granted")

    def test_over_365_days_is_refused_in_the_console(self):
        self.client.force_login(self.root)
        self.operation = op()
        response = self.client.post(self.url, self.data("confirm", days=400, confirm="on"))
        self.assertContains(response, "at most 365 days", status_code=400)
        self.assertFalse(Subscription.objects.filter(activation_source="COMPLIMENTARY").exists())

    def test_non_super_administrators_and_csrf(self):
        manager = staff("ops@example.com", "memberships.manage_memberships", "accounts.view_user")
        self.operation = op()
        self.client.force_login(manager)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, self.data("confirm", confirm="on")).status_code, 403)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.root)
        self.assertEqual(client.post(self.url, self.data("confirm", confirm="on")).status_code, 403)
        self.assertFalse(Subscription.objects.filter(activation_source="COMPLIMENTARY").exists())

    def test_member_sees_complimentary_access_not_a_purchase(self):
        granted = self.grant(days=30)
        self.client.force_login(self.member)
        mine = self.client.get(reverse("memberships:mine"))
        self.assertContains(mine, "Complimentary access: no payment was made")
        self.assertNotContains(mine, "demonstration")
        self.assertContains(mine, "Complimentary, no payment")
        self.assertNotContains(mine, "TZS 5,000")
        detail = self.client.get(reverse("memberships:subscription_detail", args=[granted.pk]))
        self.assertContains(detail, "Amount paid")
        self.assertContains(detail, "TZS 0")


@PROD
class ConcurrentProductionGrantTests(TransactionTestCase):
    """Two Super Administrators grant to one member at the same moment: one membership."""

    def setUp(self):
        enable_memberships()
        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.root2 = make_user("root2@example.com", role=User.Role.SUPER_ADMIN)
        self.member = make_user("member@example.com")
        self.plan = services.create_plan(self.root, code="monthly", name="Monthly", price_amount=5_000,
                                         duration_days=30, benefits=["Library"], entitlements=["videos.standard"])
        self.expires_at = timezone.now() + timedelta(days=30)

    def race(self, calls):
        barrier = threading.Barrier(len(calls))
        results = []

        def run(call):
            try:
                barrier.wait(5)
                results.append(call())
            except Exception as error:  # noqa: BLE001 - collected and asserted below
                results.append(error)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run, args=(call,)) for call in calls]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        return results

    def call(self, actor, operation):
        return lambda: services.grant_complimentary(actor, self.member, self.plan, expires_at=self.expires_at,
                                                    reason=REASON, operation_id=operation)

    def test_two_administrators_grant_once(self):
        results = self.race([self.call(self.root, op()), self.call(self.root2, op())])
        made = [r for r in results if isinstance(r, Subscription)]
        refused = [r for r in results if isinstance(r, services.MembershipError)]
        self.assertEqual((len(made), len(refused)), (1, 1), results)
        self.assertEqual(Subscription.objects.filter(activation_source="COMPLIMENTARY").count(), 1)

    def test_double_submission_of_one_form_grants_once(self):
        operation = op()
        results = self.race([self.call(self.root, operation), self.call(self.root, operation)])
        self.assertTrue(all(isinstance(r, Subscription) for r in results), results)
        self.assertEqual(len({r.pk for r in results}), 1)
        self.assertEqual(Subscription.objects.filter(activation_source="COMPLIMENTARY").count(), 1)
