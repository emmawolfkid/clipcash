"""Phase 14-A: time-limited complimentary demo memberships (owner decision D1)."""

import zoneinfo
from datetime import datetime, timedelta
from io import StringIO
from unittest import mock, skipUnless

from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from apps.audit.context import audit_scope
from apps.audit.models import AuditEvent
from apps.memberships import services
from apps.memberships.access import check_access
from apps.memberships.models import MembershipPlan, Subscription

from .helpers import MembershipTestCase, User, make_user, payment_for, payments_enabled
from .test_concurrency import run_threads

S = Subscription.Status
GRANTS = {"MEMBERSHIPS_COMPLIMENTARY_GRANTS_ENABLED": True, "RESTRICTED_DEMO_MODE": True}
DAR = zoneinfo.ZoneInfo("Africa/Dar_es_Salaam")

# Database-level financial and membership locks that must stay installed and enabled.
LOCK_TRIGGERS = (
    "ledger_account_guard", "ledger_line_apply", "ledger_entry_immutable", "ledger_line_immutable",
    "ledger_line_balanced", "ledger_entry_complete", "a_wallets_block_member_postings",
    "wallets_transactions_locked", "withdrawals_request_locked", "advertising_campaign_guard",
    "payments_intent_guard", "membership_subscription_guard", "audit_auditevent_append_only",
)
LOCK_CONSTRAINTS = ("adv_campaign_funding_locked", "sub_activation_source_consistent")


def staff(email, *permissions):
    """A non-superuser staff account (Content Manager role) holding ``permissions``."""
    user = make_user(email, role=User.Role.CONTENT_MANAGER)
    for dotted in permissions:
        app_label, codename = dotted.split(".")
        user.user_permissions.add(Permission.objects.get(content_type__app_label=app_label, codename=codename))
    return User.objects.get(pk=user.pk)  # Fresh permission cache.


def financial_snapshot():
    from apps.advertising.models import Campaign
    from apps.payments.models import (MembershipActivationPreview, PaymentIntent, ProviderEvent,
                                      SandboxAdvertiserFunds, SandboxCampaignAllocation, SandboxPosting)
    from apps.rewards.models import CampaignRewardBudget, DemoReward
    from apps.wallets.models import JournalEntry, JournalLine, LedgerAccount, WalletTransaction
    from apps.withdrawals.models import DemoWithdrawal, WithdrawalRequest

    models = (PaymentIntent, ProviderEvent, SandboxPosting, SandboxAdvertiserFunds, SandboxCampaignAllocation,
              MembershipActivationPreview, JournalEntry, JournalLine, WalletTransaction, DemoReward,
              CampaignRewardBudget, WithdrawalRequest, DemoWithdrawal)
    snapshot = {model.__name__: model.objects.count() for model in models}
    snapshot["ledger_balances"] = sorted(LedgerAccount.objects.values_list("code", "debit_balance"))
    snapshot["funded_campaigns"] = Campaign.objects.exclude(funding_status="UNFUNDED").count()
    return snapshot


@override_settings(**GRANTS)
class ComplimentaryTestCase(MembershipTestCase):
    def setUp(self):
        super().setUp()
        self.expiry = timezone.now() + timedelta(days=3)  # Fixed, so a repeated call is the same request.

    def grant(self, actor=None, member=None, plan=None, *, expires_at=None, reason="Client demo session",
              operation_id=""):
        return services.grant_complimentary(
            actor or self.root, member or self.member, plan or self.plan,
            expires_at=expires_at or self.expiry, reason=reason, operation_id=operation_id)


class AuthorizationTests(ComplimentaryTestCase):
    def test_super_admin_can_grant(self):
        self.assertEqual(self.grant().status, S.ACTIVE)

    def test_everyone_else_is_refused_by_the_service(self):
        callers = {
            "content manager": staff("cm@example.com"),
            "ordinary staff with membership management": staff("ops@example.com", "memberships.manage_memberships"),
            "finance-type staff": staff("fin@example.com", "payments.view_payments", "payments.approve_refunds",
                                        "wallets.view_wallets", "withdrawals.review_withdrawals"),
            "support-type staff": staff("sup@example.com", "accounts.view_user", "accounts.change_user"),
            "auditor-type staff": staff("aud@example.com", "audit.view_auditevent"),
            "member": make_user("other@example.com"),
            "the member themself": self.member,
            "suspended super admin": make_user("old-root@example.com", role=User.Role.SUPER_ADMIN),
        }
        old = callers["suspended super admin"]
        old.status = User.Status.SUSPENDED
        old.save()
        for label, caller in callers.items():
            with self.subTest(caller=label), audit_scope(), self.assertRaises(services.NotAuthorized):
                self.grant(actor=caller)
        with self.subTest(caller="anonymous"), audit_scope(), self.assertRaises(services.NotAuthorized):
            from django.contrib.auth.models import AnonymousUser

            self.grant(actor=AnonymousUser())
        self.assertFalse(Subscription.objects.filter(status=S.ACTIVE).exists())
        self.assertGreaterEqual(AuditEvent.objects.filter(action="membership.action.denied",
                                                          metadata__action="subscription.grant").count(), 9)

    def test_refused_unless_both_the_flag_and_restricted_demo_are_on(self):
        for settings_ in ({"MEMBERSHIPS_COMPLIMENTARY_GRANTS_ENABLED": False, "RESTRICTED_DEMO_MODE": True},
                          {"MEMBERSHIPS_COMPLIMENTARY_GRANTS_ENABLED": True, "RESTRICTED_DEMO_MODE": False}):
            with self.subTest(**settings_), override_settings(**settings_), audit_scope():
                self.assertFalse(services.can_grant_complimentary(self.root))
                with self.assertRaises(services.NotAuthorized) as ctx:
                    self.grant()
                self.assertEqual(ctx.exception.code, "grants_disabled")
        self.assertFalse(Subscription.objects.exists())


class ValidationTests(ComplimentaryTestCase):
    def assert_refused(self, error_class, code=None, **kwargs):
        with audit_scope(), self.assertRaises(error_class) as ctx:
            self.grant(**kwargs)
        if code:
            self.assertEqual(ctx.exception.code, code)
        self.assertFalse(Subscription.objects.filter(member=self.member, status=S.ACTIVE).exists())

    def test_missing_member_or_plan(self):
        with self.assertRaises(ValidationError):
            services.grant_complimentary(self.root, None, self.plan, expires_at=timezone.now() + timedelta(days=1),
                                         reason="Client demo")
        with self.assertRaises(ValidationError):
            services.grant_complimentary(self.root, self.member, None,
                                         expires_at=timezone.now() + timedelta(days=1), reason="Client demo")

    def test_inactive_or_archived_plan(self):
        for status in (MembershipPlan.Status.INACTIVE, MembershipPlan.Status.ARCHIVED):
            plan = self.make_plan(f"plan-{status.lower()}")
            services.set_plan_status(self.root, plan, status)
            with self.subTest(status=status):
                self.assert_refused(services.PlanUnavailable, plan=plan)

    def test_expiry_must_be_future_aware_and_within_the_plan_length(self):
        now = timezone.now()
        for label, value in (("past", now - timedelta(minutes=1)), ("now", now - timedelta(microseconds=1)),
                             ("naive", datetime(2099, 1, 1, 12, 0)), ("not a datetime", "2099-01-01"),
                             ("beyond plan", now + timedelta(days=30, minutes=5))):
            with self.subTest(expiry=label):
                self.assert_refused(ValidationError, expires_at=value)

    def test_reason_is_mandatory_and_bounded(self):
        for reason in ("", "   ", "abc", "x" * 201):
            with self.subTest(reason=reason[:10]):
                self.assert_refused(ValidationError, reason=reason)

    def test_ineligible_accounts(self):
        cases = {
            "unverified": make_user("u@example.com", verified=False),
            "suspended": make_user("s@example.com", status=User.Status.SUSPENDED),
            "disabled": make_user("d@example.com", status=User.Status.DISABLED),
            "pending": make_user("p@example.com", status=User.Status.PENDING, verified=False),
            "staff account": staff("cm@example.com"),
        }
        for label, member in cases.items():
            with self.subTest(account=label):
                self.assert_refused(services.NotEligible, member=member)

    def test_existing_active_membership_is_never_replaced(self):
        paid = self.active_membership()
        with audit_scope(), self.assertRaises(services.NotEligible) as ctx:
            self.grant()
        self.assertEqual(ctx.exception.code, "membership_exists")
        paid.refresh_from_db()
        self.assertEqual((paid.status, paid.activation_source, paid.expires_at.date() > timezone.now().date()),
                         (S.ACTIVE, "PAYMENT", True))
        self.assertEqual(Subscription.objects.filter(member=self.member).count(), 1)

    def test_suspended_membership_is_not_overridden(self):
        services.suspend(self.root, self.active_membership(), "ADMIN_REVIEW")
        with audit_scope(), self.assertRaises(services.NotEligible):
            self.grant()
        self.assertEqual(Subscription.objects.get(member=self.member).status, S.SUSPENDED)

    def test_pending_choice_of_another_plan_is_left_alone(self):
        other = self.make_plan("annual", price=50_000, days=365)
        pending = services.choose_plan(self.member, other)
        self.assert_refused(services.InvalidTransition, "pending_choice_conflict")
        pending.refresh_from_db()
        self.assertEqual(pending.status, S.PENDING)

    def test_invalid_operation_id(self):
        self.assert_refused(ValidationError, operation_id="bad id!")


class CorrectnessTests(ComplimentaryTestCase):
    def test_grant_activates_the_chosen_plan_until_the_exact_expiry(self):
        expiry = (timezone.now() + timedelta(days=5)).astimezone(DAR).replace(second=0, microsecond=0)
        granted = self.grant(expires_at=expiry, reason="  Client demo,   board meeting  ")
        granted.refresh_from_db()
        self.assertEqual((granted.status, granted.plan_id, granted.expires_at), (S.ACTIVE, self.plan.pk, expiry))
        self.assertEqual((granted.activation_source, granted.amount_paid, granted.payment_reference),
                         ("COMPLIMENTARY", 0, ""))
        self.assertEqual((granted.activated_by_id, granted.note), (self.root.pk, "Client demo, board meeting"))
        self.assertLessEqual(granted.starts_at, timezone.now())
        self.assertEqual(granted.price_amount, 5_000)  # List price kept as the plan snapshot, never "paid".

    def test_pending_choice_of_the_same_plan_is_used(self):
        pending = services.choose_plan(self.member, self.plan)
        self.assertEqual(self.grant().pk, pending.pk)

    def test_stale_pending_choice_is_cancelled_like_the_expiry_job_would(self):
        pending = services.choose_plan(self.member, self.make_plan("annual", price=50_000, days=365))
        later = timezone.now() + timedelta(days=8)
        with mock.patch("apps.memberships.services.timezone.now", return_value=later):
            granted = self.grant(expires_at=later + timedelta(days=1))
        pending.refresh_from_db()
        self.assertEqual((pending.status, pending.cancellation_reason), (S.CANCELLED, "UNPAID_TIMEOUT"))
        self.assertEqual(granted.status, S.ACTIVE)

    def test_eligibility_recognises_the_grant(self):
        self.assertEqual(check_access(self.member).reason, "no_membership")
        granted = self.grant()
        decision = check_access(self.member, entitlement="videos.standard")
        self.assertEqual((decision.allowed, decision.subscription.pk), (True, granted.pk))

    def test_audit_records_the_grant(self):
        expiry = timezone.now() + timedelta(days=2)
        granted = self.grant(expires_at=expiry, operation_id="op-12345678")
        event = AuditEvent.objects.get(action="membership.complimentary.granted")
        self.assertEqual((event.actor_id, event.target_id, event.outcome, event.category),
                         (self.root.pk, str(granted.pk), "SUCCESS", "MEMBERSHIP"))
        self.assertEqual(event.metadata, {
            "member": str(self.member.pk), "plan_code": "monthly", "starts_at": granted.starts_at.isoformat(),
            "expires_at": expiry.isoformat(), "reason": "Client demo session", "operation": "op-12345678"})
        activated = AuditEvent.objects.get(action="membership.subscription.activated")
        self.assertEqual(activated.metadata["source"], "COMPLIMENTARY")

    def test_grant_and_audit_are_one_transaction(self):
        real = services.record_business_event

        def failing(action, **kwargs):
            if action == "membership.complimentary.granted":
                raise RuntimeError("audit store unavailable")
            return real(action, **kwargs)

        with mock.patch("apps.memberships.services.record_business_event", side_effect=failing), \
                self.assertRaises(RuntimeError):
            self.grant()
        self.assertFalse(Subscription.objects.filter(member=self.member).exists())
        self.assertFalse(AuditEvent.objects.filter(action__startswith="membership.subscription").exists())


class ExpiryTests(ComplimentaryTestCase):
    def test_access_ends_exactly_at_expiry_without_the_job(self):
        expiry = timezone.now() + timedelta(hours=2)
        self.grant(expires_at=expiry)
        self.assertTrue(check_access(self.member, now=expiry - timedelta(seconds=1)))
        self.assertEqual(check_access(self.member, now=expiry).reason, "no_membership")
        self.assertEqual(Subscription.objects.get(member=self.member).status, S.ACTIVE)  # Stale status, no access.

    def test_expiry_job_marks_it_expired_and_access_stays_closed(self):
        expiry = timezone.now() + timedelta(hours=1)
        granted = self.grant(expires_at=expiry)
        self.assertEqual(services.process_expiry(now=expiry)["expired"], 1)
        granted.refresh_from_db()
        self.assertEqual(granted.status, S.EXPIRED)
        self.assertFalse(check_access(self.member, now=expiry + timedelta(days=1)))

    def test_a_new_grant_is_possible_after_expiry(self):
        expiry = timezone.now() + timedelta(hours=1)
        self.grant(expires_at=expiry)
        with mock.patch("apps.memberships.services.timezone.now", return_value=expiry + timedelta(minutes=1)):
            second = self.grant(expires_at=expiry + timedelta(days=1))
        self.assertEqual(second.status, S.ACTIVE)

    def test_timezone_offsets_are_preserved(self):
        expiry = datetime(2026, 1, 1, 18, 30, tzinfo=DAR)  # 15:30 UTC.
        now = datetime(2025, 12, 31, 9, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        with mock.patch("apps.memberships.services.timezone.now", return_value=now):
            granted = self.grant(expires_at=expiry)
        granted.refresh_from_db()
        self.assertEqual(granted.expires_at, datetime(2026, 1, 1, 15, 30, tzinfo=zoneinfo.ZoneInfo("UTC")))


class IdempotencyTests(ComplimentaryTestCase):
    def granted_events(self):
        return AuditEvent.objects.filter(action="membership.complimentary.granted").count()

    def test_same_key_and_same_request_returns_the_original_grant(self):
        first = self.grant(operation_id="form-token-1", reason="Client demo session")
        # The reason is normalised the same way on every call, so spacing does not matter.
        again = self.grant(operation_id="form-token-1", reason="  Client   demo session ")
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(Subscription.objects.filter(member=self.member).count(), 1)
        self.assertEqual(self.granted_events(), 1)

    def assert_conflict(self, **changes):
        with audit_scope(), self.assertRaises(services.InvalidTransition) as ctx:
            self.grant(operation_id="form-token-2", **changes)
        self.assertEqual(ctx.exception.code, "operation_conflict")
        self.assertEqual(str(ctx.exception), services.OPERATION_CONFLICT)  # Says nothing about the original.

    def test_same_key_with_any_different_input_is_refused_and_changes_nothing(self):
        original = self.grant(operation_id="form-token-2")
        other_member = make_user("other@example.com")
        other_root = make_user("root2@example.com", role=User.Role.SUPER_ADMIN)
        cases = {
            "expiry": {"expires_at": self.expiry + timedelta(hours=1)},
            "reason": {"reason": "A different reason"},
            "administrator": {"actor": other_root},
            "member": {"member": other_member},
            "plan": {"plan": self.make_plan("annual", price=50_000, days=365)},
        }
        for label, changes in cases.items():
            with self.subTest(changed=label):
                self.assert_conflict(**changes)
        refreshed = Subscription.objects.get(pk=original.pk)
        self.assertEqual((refreshed.status, refreshed.expires_at, refreshed.note),
                         (S.ACTIVE, original.expires_at, original.note))
        self.assertFalse(Subscription.objects.filter(member=other_member).exists())
        self.assertEqual(self.granted_events(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="membership.action.denied",
                                                   metadata__reason="operation_conflict").count(), len(cases))

    def test_unauthorized_or_invalid_retries_are_refused_before_the_key_is_used(self):
        self.grant(operation_id="form-token-3")
        with audit_scope(), self.assertRaises(services.NotAuthorized):
            self.grant(actor=staff("cm@example.com"), operation_id="form-token-3")
        with self.assertRaises(ValidationError):
            self.grant(operation_id="bad token!")
        with self.assertRaises(ValidationError):
            self.grant(operation_id="form-token-3", reason="")
        self.assertEqual(self.granted_events(), 1)

    def test_replay_of_a_cancelled_grant_is_refused(self):
        granted = self.grant(operation_id="form-token-4")
        services.cancel(self.root, granted, "ADMIN_REVIEW")
        with audit_scope(), self.assertRaises(services.InvalidTransition) as ctx:
            self.grant(operation_id="form-token-4")
        self.assertEqual(ctx.exception.code, "grant_not_active")
        self.assertIn("cancelled", str(ctx.exception))
        self.assertEqual(Subscription.objects.get(member=self.member).status, S.CANCELLED)
        self.assertFalse(check_access(self.member))
        self.assertEqual(self.granted_events(), 1)

    def test_replay_of_an_expired_grant_is_refused_with_or_without_the_job(self):
        expiry = timezone.now() + timedelta(hours=1)
        self.grant(operation_id="form-token-5", expires_at=expiry)
        after = expiry + timedelta(minutes=1)
        with mock.patch("apps.memberships.services.timezone.now", return_value=after):
            with audit_scope(), self.assertRaises(services.InvalidTransition) as ctx:  # Job not run yet.
                self.grant(operation_id="form-token-5", expires_at=expiry)
            self.assertIn("ended", str(ctx.exception))
            services.process_expiry(now=after)
            with audit_scope(), self.assertRaises(services.InvalidTransition) as ctx:
                self.grant(operation_id="form-token-5", expires_at=expiry)
            self.assertEqual(ctx.exception.code, "grant_not_active")
        self.assertEqual(Subscription.objects.get(member=self.member).status, S.EXPIRED)
        self.assertEqual(self.granted_events(), 1)

    def test_replay_of_a_suspended_grant_is_refused(self):
        granted = self.grant(operation_id="form-token-6")
        services.suspend(self.root, granted, "ADMIN_REVIEW")
        with audit_scope(), self.assertRaises(services.InvalidTransition) as ctx:
            self.grant(operation_id="form-token-6")
        self.assertIn("suspended", str(ctx.exception))

    def test_audit_layer_returning_another_grant_event_undoes_the_grant(self):
        """Defence in depth for R-01: if the grant's audit write were ever answered with a
        different (replayed) event, the activation must not commit without its own record."""
        from apps.audit.models import AuditEvent as Event

        foreign = Event(action="membership.complimentary.granted", target_id="00000000-0000-0000-0000-000000000000")
        real = services.record_business_event

        def answer(action, **kwargs):
            return foreign if action == "membership.complimentary.granted" else real(action, **kwargs)

        with mock.patch("apps.memberships.services.record_business_event", side_effect=answer), audit_scope(), \
                self.assertRaises(services.InvalidTransition) as ctx:
            self.grant(operation_id="form-token-7")
        self.assertEqual(ctx.exception.code, "operation_conflict")
        self.assertFalse(Subscription.objects.filter(member=self.member).exists())

    def test_duplicate_submission_without_operation_is_refused(self):
        self.grant()
        with audit_scope(), self.assertRaises(services.NotEligible):
            self.grant()
        self.assertEqual(Subscription.objects.filter(member=self.member, status=S.ACTIVE).count(), 1)

    def test_retry_after_an_interrupted_operation_succeeds_once(self):
        with mock.patch("apps.memberships.services._activate", side_effect=RuntimeError("connection lost")), \
                self.assertRaises(RuntimeError):
            self.grant(operation_id="form-token-3")
        self.assertFalse(Subscription.objects.filter(member=self.member).exists())
        granted = self.grant(operation_id="form-token-3")
        self.assertEqual(self.grant(operation_id="form-token-3").pk, granted.pk)


class DemoWaiverSeparationTests(ComplimentaryTestCase):
    WAIVER = {"DEMO_EMAIL_VERIFICATION_OPTIONAL": True, "DEMO_ACCOUNTS": ["demo@example.com"],
              "PAYMENTS_MODE": "disabled"}

    def test_waived_unverified_demo_account_cannot_receive_a_grant(self):
        demo = make_user("demo@example.com", verified=False)
        with override_settings(**self.WAIVER):
            from apps.accounts.access import email_verification_waived

            self.assertTrue(email_verification_waived(demo))
            with audit_scope(), self.assertRaises(services.NotEligible) as ctx:
                self.grant(member=demo)
            self.assertEqual(ctx.exception.code, "email_unverified")
            self.assertEqual(check_access(demo).reason, "no_membership")  # The waiver grants no membership.
        demo.refresh_from_db()
        self.assertFalse(demo.email_verified)  # A grant attempt never verifies the email.

    def test_verified_demo_account_can_receive_a_grant(self):
        demo = make_user("demo@example.com")
        with override_settings(**self.WAIVER):
            self.grant(member=demo)
            self.assertTrue(check_access(demo))


class FinancialInvariantTests(ComplimentaryTestCase):
    def test_grant_creates_no_money_records_and_leaves_locks_installed(self):
        from apps.wallets import wallet_services
        from apps.withdrawals import services as withdrawals

        wallet = wallet_services.ensure_wallet(self.member)
        before = financial_snapshot()
        self.grant()
        self.assertEqual(financial_snapshot(), before)
        balances = wallet_services.balances(wallet)
        self.assertEqual((balances.available, balances.pending, balances.reserved), (0, 0, 0))
        eligibility = withdrawals.withdrawal_eligibility(self.member, amount=10_000)
        self.assertFalse(eligibility)
        self.assertIn("withdrawals_locked", eligibility.reasons)
        with connection.cursor() as cursor:
            cursor.execute("SELECT tgname FROM pg_trigger WHERE tgname = ANY(%s) AND tgenabled = 'O'",
                           [list(LOCK_TRIGGERS)])
            self.assertEqual(sorted(r[0] for r in cursor.fetchall()), sorted(LOCK_TRIGGERS))
            cursor.execute("SELECT conname FROM pg_constraint WHERE conname = ANY(%s)", [list(LOCK_CONSTRAINTS)])
            self.assertEqual(sorted(r[0] for r in cursor.fetchall()), sorted(LOCK_CONSTRAINTS))

    def test_database_refuses_a_complimentary_row_that_claims_money(self):
        """Activation data is frozen by the subscription guard trigger, and the
        source/amount/reference combination by a check constraint."""
        granted = self.grant()
        from django.db import DatabaseError, transaction

        for change in ({"amount_paid": 5_000}, {"payment_reference": "FAKE-1"}, {"activation_source": "PAYMENT"},
                       {"expires_at": granted.expires_at + timedelta(days=30)}):
            with self.subTest(change=list(change)), self.assertRaises(DatabaseError), transaction.atomic():
                Subscription.objects.filter(pk=granted.pk).update(**change)
        granted.refresh_from_db()
        self.assertEqual((granted.activation_source, granted.amount_paid, granted.payment_reference),
                         ("COMPLIMENTARY", 0, ""))


@override_settings(**GRANTS)
class CommandTests(MembershipTestCase):
    def call(self, *extra):
        out = StringIO()
        call_command("grant_complimentary_membership", "--actor-email", "root@example.com", "--member-email",
                     "member@example.com", "--plan-code", "monthly", *extra, stdout=out)
        return out.getvalue()

    def test_command_uses_the_same_service(self):
        expiry = (timezone.now() + timedelta(days=2)).astimezone(DAR).replace(microsecond=0)
        output = self.call("--expires-at", expiry.isoformat(), "--reason", "CLI demo grant")
        self.assertIn("(no payment)", output)
        granted = Subscription.objects.get(member=self.member)
        self.assertEqual((granted.activation_source, granted.expires_at, granted.note),
                         ("COMPLIMENTARY", expiry, "CLI demo grant"))
        self.assertEqual(AuditEvent.objects.get(action="membership.complimentary.granted").source, "CLI")

    def test_command_refuses_naive_expiry_short_reason_and_unauthorized_actor(self):
        future = (timezone.now() + timedelta(days=2)).replace(tzinfo=None).isoformat()
        with self.assertRaises(CommandError):
            self.call("--expires-at", future, "--reason", "CLI demo grant")
        aware = (timezone.now() + timedelta(days=2)).isoformat()
        with self.assertRaises(CommandError):
            self.call("--expires-at", aware, "--reason", "no")
        make_user("cm@example.com", role=User.Role.CONTENT_MANAGER)
        with self.assertRaises(CommandError):
            call_command("grant_complimentary_membership", "--actor-email", "cm@example.com", "--member-email",
                         "member@example.com", "--plan-code", "monthly", "--expires-at", aware,
                         "--reason", "CLI demo grant", stdout=StringIO())
        self.assertFalse(Subscription.objects.exists())


@skipUnless(connection.vendor == "postgresql", "Concurrency is verified on PostgreSQL.")
@override_settings(**GRANTS)
class ComplimentaryConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.member = make_user("member@example.com")
        gate = mock.patch("apps.memberships.services._feature_enabled", return_value=True)
        gate.start()
        self.addCleanup(gate.stop)
        self.plan = services.create_plan(self.root, code="monthly", name="Monthly", price_amount=5_000,
                                         duration_days=30, entitlements=["videos.standard"])
        self.expiry = timezone.now() + timedelta(days=3)

    def grant(self, operation_id=""):
        with audit_scope():
            return services.grant_complimentary(self.root, self.member, self.plan, expires_at=self.expiry,
                                                reason="Concurrent demo grant", operation_id=operation_id)

    def test_concurrent_grants_activate_exactly_one(self):
        results = run_threads(5, lambda i: self.grant(f"operation-{i}0000"))
        granted = [r for r in results if isinstance(r, Subscription)]
        self.assertEqual(len(granted), 1, results)
        self.assertTrue(all(isinstance(r, services.NotEligible) for r in results if r not in granted), results)
        self.assertEqual(Subscription.objects.filter(member=self.member, status=S.ACTIVE).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="membership.complimentary.granted").count(), 1)

    def test_concurrent_retries_of_one_operation_return_the_same_grant(self):
        results = run_threads(4, lambda i: self.grant("same-operation-1"))
        self.assertTrue(all(isinstance(r, Subscription) for r in results), results)
        self.assertEqual(len({r.pk for r in results}), 1)
        self.assertEqual(AuditEvent.objects.filter(action="membership.complimentary.granted").count(), 1)

    def assert_one_key_one_grant(self, rounds, make_call):
        """R-01: one retry key never authorises two grants, whatever the members or plans,
        and every successful grant has its own grant audit event."""
        for round_ in range(rounds):
            key = f"shared-key-{round_:04d}"
            calls = make_call(round_)
            results = run_threads(len(calls), lambda i: calls[i](key))
            granted = [r for r in results if isinstance(r, Subscription)]
            refused = [r for r in results if r not in granted]
            with self.subTest(round=round_):
                self.assertEqual(len(granted), 1, results)
                self.assertTrue(all(isinstance(r, services.InvalidTransition) and r.code == "operation_conflict"
                                    for r in refused), results)
                events = AuditEvent.objects.filter(action="membership.complimentary.granted",
                                                   idempotency_key=f"membership.complimentary:{key}")
                self.assertEqual(events.count(), 1)
                self.assertEqual(events.get().target_id, str(granted[0].pk))
        complimentary = Subscription.objects.filter(activation_source="COMPLIMENTARY").count()
        self.assertEqual(complimentary, rounds)  # Refused requests left no activation behind.
        self.assertEqual(AuditEvent.objects.filter(action="membership.complimentary.granted").count(), complimentary)

    def test_same_key_for_different_members_concurrently_grants_once(self):
        def calls(round_):
            members = [make_user(f"race{round_}-{i}@example.com") for i in range(3)]

            def call(member):
                def run(key):
                    with audit_scope():
                        return services.grant_complimentary(self.root, member, self.plan, expires_at=self.expiry,
                                                            reason="Concurrent demo grant", operation_id=key)
                return run
            return [call(member) for member in members]

        self.assert_one_key_one_grant(5, calls)
        self.assertEqual(Subscription.objects.exclude(activation_source="COMPLIMENTARY").count(), 0)

    def test_same_key_for_different_plans_concurrently_grants_once(self):
        plans = [self.plan] + [services.create_plan(self.root, code=f"plan-{i}", name=f"Plan {i}", price_amount=5_000,
                                                    duration_days=30, entitlements=["videos.standard"])
                               for i in range(2)]

        def calls(round_):
            member = make_user(f"plans{round_}@example.com")

            def call(plan):
                def run(key):
                    with audit_scope():
                        return services.grant_complimentary(self.root, member, plan, expires_at=self.expiry,
                                                            reason="Concurrent demo grant", operation_id=key)
                return run
            return [call(plan) for plan in plans]

        self.assert_one_key_one_grant(5, calls)
        self.assertFalse(Subscription.objects.filter(status=S.PENDING).exists())  # No partial pending left.

    def test_grant_racing_a_verified_payment_activates_once(self):
        pending = services.choose_plan(self.member, self.plan)

        def act(i):
            if i == 0:
                return self.grant()
            with payments_enabled():
                return services.activate_with_verified_payment(pending.pk, payment_for(pending, reference="PAY-1"))

        results = run_threads(2, act)
        self.assertEqual(sum(isinstance(r, Subscription) for r in results), 1, results)
        live = Subscription.objects.filter(member=self.member, status=S.ACTIVE)
        self.assertEqual(live.count(), 1)
        self.assertIn(live.get().activation_source, ("COMPLIMENTARY", "PAYMENT"))

    def test_grant_racing_a_member_plan_choice_stays_consistent(self):
        other = services.create_plan(self.root, code="annual", name="Annual", price_amount=50_000,
                                     duration_days=365, entitlements=["videos.standard"])

        def act(i):
            if i == 0:
                return self.grant()
            return services.choose_plan(self.member, other)

        results = run_threads(2, act)
        active = Subscription.objects.filter(member=self.member, status=S.ACTIVE)
        if isinstance(results[0], Subscription):  # Grant first: the later choice is a too-early renewal.
            self.assertIsInstance(results[1], services.RenewalNotAllowed)
            self.assertEqual(active.count(), 1)
        else:  # Choice first: the grant leaves the other pending choice alone.
            self.assertIsInstance(results[0], services.InvalidTransition)
            self.assertFalse(active.exists())
            self.assertEqual(Subscription.objects.get(member=self.member).status, S.PENDING)
