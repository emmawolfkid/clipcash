"""PAY-LIVE-01D: Operations Console membership management (suspend, reinstate, cancel, extend).

Services: authorization, reasons, state rules, eligibility, extension bounds, idempotency,
stale-form protection, MariaDB/PostgreSQL guard compatibility, audit and "no money moved".
Console: what each administrator sees and may do, confirmations, CSRF and POST-only changes.
"""

import threading
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connections, transaction
from django.test import Client, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.memberships import services
from apps.memberships.access import check_access
from apps.memberships.models import Subscription
from apps.memberships.tests.helpers import (
    MembershipTestCase,
    User,
    enable_memberships,
    make_user,
    payment_for,
    payments_enabled,
)
from apps.payments.models import ManualMembershipPayment, PaymentIntent
from apps.wallets.models import JournalEntry, Wallet

S = Subscription.Status


def staff(email, *permissions):
    user = make_user(email, role=User.Role.CONTENT_MANAGER)
    for dotted in permissions:
        app_label, codename = dotted.split(".")
        user.user_permissions.add(Permission.objects.get(content_type__app_label=app_label, codename=codename))
    return User.objects.get(pk=user.pk)


def money_records():
    return {model.__name__: model.objects.count()
            for model in (JournalEntry, ManualMembershipPayment, PaymentIntent, Wallet)}


def op():
    return uuid.uuid4().hex


class ManagementTestCase(MembershipTestCase):
    def setUp(self):
        super().setUp()
        self.subscription = self.active_membership()
        self.manager = staff("ops@example.com", "memberships.manage_memberships", "accounts.view_user")
        self.viewer = staff("viewer@example.com", "accounts.view_user")

    def extend(self, days=10, reason="Service outage credit", actor=None, operation=None, expected_end=None,
               subscription=None):
        subscription = subscription or self.subscription
        if expected_end is None:
            expected_end = services.membership_end(subscription.member).isoformat()
        return services.extend(actor or self.root, subscription, days=days, reason=reason,
                               operation_id=operation or op(), expected_end=expected_end)


# --- Services -------------------------------------------------------------------------------


class SuspendReinstateCancelTests(ManagementTestCase):
    def test_suspend_blocks_access_and_reinstate_restores_it(self):
        services.suspend(self.manager, self.subscription, "ADMIN_REVIEW", note="Chargeback under review")
        self.assertEqual(check_access(self.member).reason, "membership_suspended")
        event = AuditEvent.objects.get(action="membership.subscription.suspended")
        self.assertEqual((event.actor_id, event.metadata["reason"], event.metadata["note"]),
                         (self.manager.pk, "ADMIN_REVIEW", "Chargeback under review"))
        services.reinstate(self.manager, self.subscription, note="Review closed")
        self.assertEqual(check_access(self.member).reason, "ok")
        self.assertEqual(AuditEvent.objects.get(action="membership.subscription.reinstated").metadata["note"],
                         "Review closed")

    def test_cancel_ends_access_without_refund_or_ledger_change(self):
        before = money_records()
        services.cancel(self.manager, self.subscription, "REFUND", note="Refunded manually by phone")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, S.CANCELLED)
        self.assertEqual(check_access(self.member).reason, "no_membership")
        self.assertEqual(self.subscription.amount_paid, self.subscription.price_amount)  # Record untouched.
        self.assertEqual(money_records(), before)

    def test_a_valid_reason_is_required(self):
        for bad in ("", "WHATEVER", "UNPAID_TIMEOUT"):
            with self.subTest(reason=bad), self.assertRaises(ValidationError):
                services.suspend(self.manager, self.subscription, bad)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, S.ACTIVE)

    def test_unauthorized_staff_and_members_are_refused_and_audited(self):
        for actor in (self.viewer, self.member):
            with self.subTest(actor=actor.email):
                with self.assertRaises(services.NotAuthorized):
                    services.suspend(actor, self.subscription, "ADMIN_REVIEW")
                with self.assertRaises(services.NotAuthorized):
                    self.extend(actor=actor)
        self.assertTrue(AuditEvent.objects.filter(action="membership.action.denied").exists())
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, S.ACTIVE)

    def test_administrators_cannot_change_their_own_membership(self):
        own = self.active_membership(member=self.manager)
        with self.assertRaises(services.NotAuthorized) as caught:
            services.suspend(self.manager, own, "ADMIN_REVIEW")
        self.assertEqual(caught.exception.code, "own_membership")

    def test_reinstating_an_ended_period_expires_it_instead(self):
        services.suspend(self.manager, self.subscription, "ADMIN_REVIEW")
        later = self.subscription.expires_at + timedelta(seconds=1)
        with mock.patch("apps.memberships.services.timezone", SimpleNamespace(now=lambda: later)):
            result = services.reinstate(self.manager, self.subscription, note="Too late")
        self.assertEqual(result.status, S.EXPIRED)
        self.assertNotEqual(check_access(self.member).reason, "ok")

    def test_reinstating_an_ineligible_account_is_refused(self):
        services.suspend(self.manager, self.subscription, "ADMIN_REVIEW")
        User.objects.filter(pk=self.member.pk).update(status=User.Status.SUSPENDED, is_active=False)
        with self.assertRaises(services.NotEligible):
            services.reinstate(self.manager, self.subscription, note="Try anyway")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, S.SUSPENDED)

    def test_expired_and_cancelled_periods_cannot_change(self):
        services.process_expiry(now=self.subscription.expires_at + timedelta(seconds=1))
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, S.EXPIRED)
        for call in (lambda: services.suspend(self.manager, self.subscription, "ADMIN_REVIEW"),
                     lambda: services.cancel(self.manager, self.subscription, "ADMIN_REVIEW")):
            with self.assertRaises(services.InvalidTransition):
                call()


class ExtensionTests(ManagementTestCase):
    def test_extension_appends_a_non_payment_period_and_audits_it(self):
        before = money_records()
        previous_end = self.subscription.expires_at
        extension, created = self.extend(days=14, reason="Service outage credit")
        self.assertTrue(created)
        self.assertEqual((extension.status, extension.activation_source, extension.amount_paid,
                          extension.payment_reference), (S.ACTIVE, "EXTENSION", 0, ""))
        self.assertEqual((extension.starts_at, extension.expires_at), (previous_end, previous_end + timedelta(days=14)))
        self.assertEqual((extension.plan_id, extension.plan_version_id, extension.entitlements),
                         (self.subscription.plan_id, self.subscription.plan_version_id, self.subscription.entitlements))
        self.assertEqual(extension.note, "Service outage credit")
        # The original period and every money record are untouched.
        original = Subscription.objects.get(pk=self.subscription.pk)
        self.assertEqual((original.expires_at, original.amount_paid, original.payment_reference, original.status),
                         (previous_end, self.subscription.amount_paid, self.subscription.payment_reference, S.ACTIVE))
        self.assertEqual(money_records(), before)
        event = AuditEvent.objects.get(action=services.EXTENSION_ACTION)
        self.assertEqual(event.actor_id, self.root.pk)
        self.assertEqual(event.target_id, str(extension.pk))
        for key, value in {"member": str(self.member.pk), "extended_subscription": str(self.subscription.pk),
                           "days": 14, "reason": "Service outage credit",
                           "previous_expires_at": previous_end.isoformat(),
                           "new_expires_at": extension.expires_at.isoformat(),
                           "adjustment": "administrative_non_payment", "amount_paid": 0}.items():
            self.assertEqual(event.metadata[key], value, key)
        # Access continues seamlessly past the original end, and only until the new end.
        self.assertEqual(check_access(self.member, now=previous_end + timedelta(days=1)).reason, "ok")
        self.assertEqual(check_access(self.member, now=extension.expires_at + timedelta(seconds=1)).reason,
                         "no_membership")
        self.assertTrue(self.member.notifications.filter(kind="membership.extended").exists())

    def test_duration_and_reason_are_bounded(self):
        for days in (0, -3, services.EXTENSION_MAX_DAYS + 1, "abc", None):
            with self.subTest(days=days), self.assertRaises(ValidationError):
                self.extend(days=days)
        for reason in ("", "abc", "x" * (services.GRANT_REASON_MAX + 1)):
            with self.subTest(reason=len(reason)), self.assertRaises(ValidationError):
                self.extend(reason=reason)
        self.assertFalse(Subscription.objects.filter(activation_source="EXTENSION").exists())
        extension, _created = self.extend(days=services.EXTENSION_MAX_DAYS)
        self.assertEqual(extension.expires_at - extension.starts_at, timedelta(days=services.EXTENSION_MAX_DAYS))

    def test_only_super_administrators_extend(self):
        with self.assertRaises(services.NotAuthorized):
            self.extend(actor=self.manager)  # May suspend and cancel, but not extend.

    def test_repeated_submission_returns_the_original_extension(self):
        operation = op()
        expected = services.membership_end(self.member).isoformat()
        first, created = self.extend(operation=operation, expected_end=expected)
        again, created_again = self.extend(operation=operation, expected_end=expected)
        self.assertEqual((created, created_again, again.pk), (True, False, first.pk))
        self.assertEqual(Subscription.objects.filter(activation_source="EXTENSION").count(), 1)
        with self.assertRaises(services.InvalidTransition) as caught:  # Same form, different request.
            self.extend(operation=operation, expected_end=expected, days=20)
        self.assertEqual(caught.exception.code, "operation_conflict")

    def test_a_second_administrator_with_a_stale_page_changes_nothing(self):
        stale = services.membership_end(self.member).isoformat()
        self.extend(expected_end=stale)
        other_admin = make_user("root2@example.com", role=User.Role.SUPER_ADMIN)
        with self.assertRaises(services.InvalidTransition) as caught:
            self.extend(actor=other_admin, expected_end=stale)
        self.assertEqual(caught.exception.code, "stale_extension")
        self.assertEqual(Subscription.objects.filter(activation_source="EXTENSION").count(), 1)

    def test_extensions_chain_after_each_other(self):
        first, _ = self.extend(days=5)
        second, _ = self.extend(days=7)
        self.assertEqual((second.starts_at, second.expires_at), (first.expires_at, first.expires_at + timedelta(days=7)))

    def test_ineligible_memberships_are_not_extended(self):
        cases = {}
        suspended = self.subscription
        services.suspend(self.root, suspended, "ADMIN_REVIEW")
        cases["suspended"] = suspended
        for name, subscription in cases.items():
            with self.subTest(name), self.assertRaises(services.InvalidTransition):
                self.extend(subscription=subscription)
        services.cancel(self.root, suspended, "ADMIN_REVIEW")
        with self.assertRaises(services.InvalidTransition):
            self.extend(subscription=suspended, expected_end="")
        other = make_user("other@example.com")
        pending = services.choose_plan(other, self.plan)
        with self.assertRaises(services.InvalidTransition):
            self.extend(subscription=pending, expected_end="")
        self.assertFalse(Subscription.objects.filter(activation_source="EXTENSION").exists())

    def test_expired_membership_is_not_extended(self):
        services.process_expiry(now=self.subscription.expires_at + timedelta(seconds=1))
        with self.assertRaises(services.InvalidTransition):
            self.extend(expected_end="")

    def test_open_plan_choice_or_inactive_account_blocks_extension(self):
        with transaction.atomic():  # A renewal choice waiting for payment (made inside the renewal window).
            pending = services._new_pending(self.member, self.make_plan("annual", days=365), timezone.now(),
                                            renewal_of=self.subscription)
        with self.assertRaises(services.InvalidTransition) as caught:
            self.extend()
        self.assertEqual(caught.exception.code, "pending_choice")
        services.cancel_pending(self.member, pending)
        User.objects.filter(pk=self.member.pk).update(status=User.Status.SUSPENDED, is_active=False)
        with self.assertRaises(services.NotEligible):
            self.extend()

    def test_database_guards_still_forbid_editing_a_period(self):
        """Why extensions are separate periods: activated periods are immutable in the database."""
        with self.assertRaises(DatabaseError), transaction.atomic():
            Subscription.objects.filter(pk=self.subscription.pk).update(
                expires_at=self.subscription.expires_at + timedelta(days=1))
        self.extend()  # The supported path passes every guard trigger.

    def test_extension_rows_need_zero_payment_and_no_reference(self):
        extension, _ = self.extend()
        with self.assertRaises(DatabaseError), transaction.atomic():
            Subscription.objects.filter(pk=extension.pk).update(amount_paid=5_000)


class ConcurrentExtensionTests(TransactionTestCase):
    """Two administrators submit at the same moment: exactly one extension is made."""

    def setUp(self):
        enable_memberships()
        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.root2 = make_user("root2@example.com", role=User.Role.SUPER_ADMIN)
        self.member = make_user("member@example.com")
        plan = services.create_plan(self.root, code="monthly", name="Monthly", price_amount=5_000, duration_days=30,
                                    benefits=["Library"], entitlements=["videos.standard"])
        pending = services.choose_plan(self.member, plan)
        with payments_enabled():
            self.subscription = services.activate_with_verified_payment(pending.pk, payment_for(pending))

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

    def call(self, actor, operation, expected):
        return lambda: services.extend(actor, self.subscription, days=10, reason="Outage credit",
                                       operation_id=operation, expected_end=expected)

    def test_two_administrators_extend_once(self):
        expected = services.membership_end(self.member).isoformat()
        results = self.race([self.call(self.root, op(), expected), self.call(self.root2, op(), expected)])
        made = [r for r in results if isinstance(r, tuple)]
        refused = [r for r in results if isinstance(r, services.MembershipError)]
        self.assertEqual((len(made), len(refused)), (1, 1), results)
        self.assertEqual(Subscription.objects.filter(activation_source="EXTENSION").count(), 1)

    def test_double_submission_of_one_form_extends_once(self):
        expected = services.membership_end(self.member).isoformat()
        operation = op()
        results = self.race([self.call(self.root, operation, expected), self.call(self.root, operation, expected)])
        self.assertTrue(all(isinstance(r, tuple) for r in results), results)
        self.assertEqual(sorted(created for _sub, created in results), [False, True])
        self.assertEqual(Subscription.objects.filter(activation_source="EXTENSION").count(), 1)


# --- Operations Console -------------------------------------------------------------------------


class ConsoleTests(ManagementTestCase):
    def setUp(self):
        super().setUp()
        self.page = reverse("administration:member", args=[self.member.pk])

    def action_url(self, action, subscription=None):
        return reverse("administration:member_membership_action",
                       args=[self.member.pk, (subscription or self.subscription).pk, action])

    def test_member_page_shows_membership_details_history_and_audit(self):
        services.suspend(self.root, self.subscription, "ADMIN_REVIEW", note="Checking a dispute")
        services.reinstate(self.root, self.subscription, note="Dispute resolved")
        self.client.force_login(self.root)
        page = self.client.get(self.page)
        self.assertContains(page, "Current membership")
        self.assertContains(page, "Verified payment")
        self.assertContains(page, "left)")
        self.assertContains(page, "Membership history")
        self.assertContains(page, "membership.subscription.reinstated")
        self.assertContains(page, "Dispute resolved")
        for action in ("suspend", "cancel", "extend"):
            self.assertContains(page, self.action_url(action))
        self.assertNotContains(page, self.action_url("reinstate"))

    def test_viewers_see_details_but_no_actions_and_cannot_act(self):
        self.client.force_login(self.viewer)
        page = self.client.get(self.page)
        self.assertContains(page, "Current membership")
        self.assertNotContains(page, f"/members/{self.member.pk}/memberships/")  # No action links.
        for action in ("suspend", "cancel", "reinstate", "extend"):
            with self.subTest(action=action):
                self.assertEqual(self.client.get(self.action_url(action)).status_code, 403)
                self.assertEqual(self.client.post(self.action_url(action), {}).status_code, 403)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, S.ACTIVE)

    def test_membership_managers_cannot_extend(self):
        self.client.force_login(self.manager)
        page = self.client.get(self.page)
        self.assertContains(page, self.action_url("suspend"))
        self.assertNotContains(page, self.action_url("extend"))
        self.assertEqual(self.client.get(self.action_url("extend")).status_code, 403)

    def test_members_cannot_reach_the_console(self):
        self.client.force_login(self.member)
        response = self.client.post(self.action_url("cancel"), {"reason": "ADMIN_REVIEW", "note": "Self service",
                                                                "confirm": "on"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response["Location"])
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, S.ACTIVE)

    def test_suspend_needs_reason_note_and_confirmation(self):
        self.client.force_login(self.root)
        form = self.client.get(self.action_url("suspend"))
        self.assertContains(form, "csrfmiddlewaretoken")
        self.assertContains(form, "Access to membership content stops immediately")
        refused = self.client.post(self.action_url("suspend"), {"reason": "", "note": "", })
        self.assertEqual(refused.status_code, 400)
        self.assertContains(refused, "Tick the confirmation box", status_code=400)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, S.ACTIVE)
        done = self.client.post(self.action_url("suspend"), {"reason": "FRAUD_SUSPECTED", "note": "Card dispute",
                                                             "confirm": "on"})
        self.assertRedirects(done, self.page)
        self.subscription.refresh_from_db()
        self.assertEqual((self.subscription.status, self.subscription.suspension_reason), (S.SUSPENDED,
                                                                                          "FRAUD_SUSPECTED"))

    def test_reinstate_and_cancel_from_the_console(self):
        services.suspend(self.root, self.subscription, "ADMIN_REVIEW")
        self.client.force_login(self.manager)
        self.assertRedirects(self.client.post(self.action_url("reinstate"), {"note": "Review done", "confirm": "on"}),
                             self.page)
        self.assertEqual(check_access(self.member).reason, "ok")
        cancelled = self.client.post(self.action_url("cancel"), {"reason": "POLICY_VIOLATION", "note": "Shared account",
                                                                 "confirm": "on"}, follow=True)
        self.assertContains(cancelled, "No refund or ledger reversal was made.")
        self.assertEqual(check_access(self.member).reason, "no_membership")

    def test_unavailable_actions_are_refused(self):
        self.client.force_login(self.root)
        response = self.client.get(self.action_url("reinstate"), follow=True)  # Active, not suspended.
        self.assertContains(response, "That action is not available")
        posted = self.client.post(self.action_url("reinstate"), {"note": "Not suspended", "confirm": "on"})
        self.assertEqual(posted.status_code, 400)
        self.assertContains(posted, "Only suspended subscriptions can be reinstated", status_code=400)
        self.assertEqual(self.client.get(self.action_url("delete")).status_code, 403)

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.root)
        response = client.post(self.action_url("suspend"), {"reason": "ADMIN_REVIEW", "note": "No token",
                                                            "confirm": "on"})
        self.assertEqual(response.status_code, 403)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, S.ACTIVE)

    def test_extend_from_the_console_once_even_if_submitted_twice(self):
        self.client.force_login(self.root)
        form = self.client.get(self.action_url("extend"))
        self.assertContains(form, "no payment was received")
        initial = form.context["form"].initial
        data = {"days": "30", "reason": "Goodwill after outage", "confirm": "on", **initial}
        first = self.client.post(self.action_url("extend"), data, follow=True)
        self.assertContains(first, "No payment was recorded.")
        again = self.client.post(self.action_url("extend"), data, follow=True)
        self.assertContains(again, "This extension was already made")
        self.assertEqual(Subscription.objects.filter(activation_source="EXTENSION").count(), 1)
        page = self.client.get(self.page)
        self.assertContains(page, "Administrative extension (no payment)")

    def test_second_administrator_sees_a_clear_stale_page_error(self):
        other = make_user("root2@example.com", role=User.Role.SUPER_ADMIN)
        self.client.force_login(self.root)
        mine = self.client.get(self.action_url("extend")).context["form"].initial
        second = Client()
        second.force_login(other)
        theirs = second.get(self.action_url("extend")).context["form"].initial
        self.client.post(self.action_url("extend"), {"days": "10", "reason": "First credit", "confirm": "on", **mine})
        late = second.post(self.action_url("extend"), {"days": "10", "reason": "Second credit", "confirm": "on",
                                                       **theirs})
        self.assertEqual(late.status_code, 400)
        self.assertContains(late, "changed after you opened the page", status_code=400)
        self.assertEqual(Subscription.objects.filter(activation_source="EXTENSION").count(), 1)

    def test_member_pages_never_show_a_price_for_an_extension(self):
        extension, _ = self.extend(days=10)
        self.client.force_login(self.member)
        mine = self.client.get(reverse("memberships:mine"))
        self.assertContains(mine, "Extension, no payment")
        detail = self.client.get(reverse("memberships:subscription_detail", args=[extension.pk]))
        self.assertContains(detail, "Amount paid")
        self.assertContains(detail, "TZS 0")


# --- Lifecycle safeguard: scheduled extensions follow revocation (PAY-LIVE-01D) -------------------


class ScheduledExtensionSafeguardTests(ManagementTestCase):
    """A scheduled EXTENSION period must never restore access after an administrator cancels or
    suspends the membership, including at the moment the revoked period would have ended."""

    def setUp(self):
        super().setUp()
        self.end = self.subscription.expires_at  # Where the extension begins.
        self.extension, _ = self.extend(days=20)

    def reason_at(self, moment):
        return check_access(self.member, now=moment).reason

    def boundary(self):
        """Just before, exactly at and inside the extension's start."""
        return (self.end - timedelta(seconds=1), self.end, self.end + timedelta(days=1))

    def notices(self, kind):
        from apps.notifications.models import Notification

        return Notification.objects.filter(recipient=self.member, kind=kind).count()

    def test_without_revocation_the_extension_continues_access_at_the_boundary(self):
        for moment in self.boundary():
            self.assertEqual(self.reason_at(moment), "ok", moment)

    def test_cancelling_the_current_period_cancels_the_scheduled_extension(self):
        before = money_records()
        services.cancel(self.manager, self.subscription, "POLICY_VIOLATION", note="Account sharing")
        self.extension.refresh_from_db()
        self.assertEqual((self.extension.status, self.extension.cancellation_reason),
                         (S.CANCELLED, "POLICY_VIOLATION"))
        for moment in self.boundary():
            self.assertEqual(self.reason_at(moment), "no_membership", moment)
        cascade = AuditEvent.objects.get(action="membership.subscription.cancelled",
                                         target_id=str(self.extension.pk))
        self.assertEqual((cascade.metadata["cascade_of"], cascade.metadata["note"], cascade.actor_id),
                         (str(self.subscription.pk), "Account sharing", self.manager.pk))
        self.assertTrue(Subscription.objects.filter(pk=self.extension.pk).exists())  # Kept, never deleted.
        self.assertEqual(self.notices("membership.cancelled"), 1)  # One notice for one decision.
        self.assertEqual(money_records(), before)

    def test_chained_extensions_are_all_cancelled(self):
        second, _ = self.extend(days=5)
        services.cancel(self.root, self.subscription, "ADMIN_REVIEW")
        statuses = set(Subscription.objects.filter(activation_source="EXTENSION").values_list("status", flat=True))
        self.assertEqual(statuses, {S.CANCELLED})
        self.assertEqual(self.reason_at(second.starts_at + timedelta(hours=1)), "no_membership")

    def test_suspending_the_current_period_suspends_the_scheduled_extension(self):
        services.suspend(self.manager, self.subscription, "FRAUD_SUSPECTED", note="Card dispute")
        self.extension.refresh_from_db()
        self.assertEqual(self.extension.status, S.SUSPENDED)
        for moment in self.boundary():
            self.assertEqual(self.reason_at(moment), "membership_suspended", moment)
        self.assertEqual(self.notices("membership.suspended"), 1)
        with self.assertRaises(services.InvalidTransition):  # No new extension while suspended.
            self.extend()

    def test_reinstating_restores_the_extensions_suspended_with_it(self):
        services.suspend(self.manager, self.subscription, "ADMIN_REVIEW")
        services.reinstate(self.manager, self.subscription, note="Dispute resolved")
        self.extension.refresh_from_db()
        self.assertEqual(self.extension.status, S.ACTIVE)
        for moment in self.boundary():
            self.assertEqual(self.reason_at(moment), "ok", moment)
        self.assertEqual(self.notices("membership.reinstated"), 1)

    def test_an_extension_suspended_separately_stays_suspended(self):
        services.suspend(self.root, self.extension, "ADMIN_REVIEW", note="Extension under review")
        services.suspend(self.root, self.subscription, "ADMIN_REVIEW")
        services.reinstate(self.root, self.subscription)
        self.extension.refresh_from_db()
        self.assertEqual(self.extension.status, S.SUSPENDED)
        self.assertEqual(self.reason_at(self.end - timedelta(seconds=1)), "ok")
        self.assertEqual(self.reason_at(self.end + timedelta(days=1)), "membership_suspended")

    def test_account_level_suspension_already_blocks_every_period(self):
        """No extra state is needed for account suspensions: eligibility is checked on every access."""
        User.objects.filter(pk=self.member.pk).update(status=User.Status.SUSPENDED, is_active=False)
        self.member.refresh_from_db()  # Requests always load the account afresh.
        for moment in self.boundary():
            self.assertEqual(self.reason_at(moment), "account_inactive", moment)

    def test_replaying_the_extension_form_after_cancellation_adds_nothing(self):
        operation = op()
        expected = services.membership_end(self.member).isoformat()
        extra, _ = self.extend(days=3, operation=operation, expected_end=expected)
        services.cancel(self.root, self.subscription, "ADMIN_REVIEW")
        replay, created = self.extend(days=3, operation=operation, expected_end=expected)
        self.assertEqual((replay.pk, created, replay.status), (extra.pk, False, S.CANCELLED))
        self.assertEqual(Subscription.objects.filter(activation_source="EXTENSION").count(), 2)


class PaidRenewalPreservedTests(MembershipTestCase):
    """Cancelling the current period cancels later extensions only; a paid renewal is kept."""

    def test_paid_renewal_survives_and_only_the_extension_is_cancelled(self):
        short = self.make_plan("weekly", price=2_000, days=7)
        current = self.active_membership(plan=short)
        renewal = self.activate(services.choose_plan(self.member, short))  # Paid, queued after current.
        self.assertEqual((renewal.status, renewal.activation_source, renewal.starts_at),
                         (S.ACTIVE, "PAYMENT", current.expires_at))
        extension, _ = services.extend(self.root, current, days=10, reason="Goodwill credit", operation_id=op(),
                                       expected_end=services.membership_end(self.member).isoformat())
        self.assertEqual(extension.starts_at, renewal.expires_at)
        before = money_records()
        services.cancel(self.root, current, "ADMIN_REVIEW", note="Wrong plan")
        renewal.refresh_from_db()
        extension.refresh_from_db()
        self.assertEqual((renewal.status, renewal.amount_paid), (S.ACTIVE, 2_000))
        self.assertEqual(extension.status, S.CANCELLED)
        self.assertEqual(check_access(self.member, now=renewal.starts_at + timedelta(hours=1)).reason, "ok")
        self.assertEqual(check_access(self.member, now=extension.starts_at + timedelta(hours=1)).reason,
                         "no_membership")
        self.assertEqual(money_records(), before)


class SafeguardConsoleTests(ManagementTestCase):
    def test_confirmation_page_says_extensions_follow_and_renewals_do_not(self):
        self.extend(days=10)
        self.client.force_login(self.root)
        url = reverse("administration:member_membership_action",
                      args=[self.member.pk, self.subscription.pk, "cancel"])
        page = self.client.get(url)
        self.assertContains(page, "1 later administrative extension follows this change")
        self.assertNotContains(page, "other later period")
