"""PAY-01B: administrator-configured plans (any price, any duration), quotes protected
against later plan changes, verified activation, early and expired renewal, expiry."""

from datetime import timedelta
from unittest import mock, skipUnless

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.helpers import User, make_user
from apps.audit.models import AuditEvent
from apps.core.models import SiteConfiguration
from apps.memberships import services as memberships
from apps.memberships.access import check_access
from apps.memberships.models import MembershipPlan, MembershipPlanVersion, Subscription
from apps.memberships.tests.test_concurrency import run_threads
from apps.payments import manual

from .manual_helpers import MANUAL_ON, ManualPaymentTestCase, staff_with

PLANS = reverse("administration:plans")
CREATE = reverse("administration:plan_create")


def plan_url(plan):
    return reverse("administration:plan", args=[plan.pk])


def form_data(plan=None, **values):
    data = {"name": "Quarterly", "description": "Three months of standard videos.", "price_amount": "12000",
            "duration_days": "90", "display_order": "20", "is_public": "on", "benefits_text": "Standard library",
            "entitlement_codes": ["videos.standard"]}
    if plan is None:
        data["code"] = "quarterly"
    else:
        data["expected_version"] = str(plan.version)
    data.update(values)
    return data


class PlanConsoleTests(ManualPaymentTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.root)

    def test_create_edit_and_audit_through_the_console(self):
        response = self.client.post(CREATE, form_data())
        plan = MembershipPlan.objects.get(code="quarterly")
        self.assertRedirects(response, plan_url(plan))
        self.assertEqual((plan.price_amount, plan.duration_days, plan.status, plan.version), (12_000, 90, "ACTIVE", 1))

        response = self.client.post(plan_url(plan), form_data(plan, price_amount="15000", duration_days="120"))
        self.assertRedirects(response, plan_url(plan))
        plan.refresh_from_db()
        self.assertEqual((plan.price_amount, plan.duration_days, plan.version), (15_000, 120, 2))
        self.assertEqual(list(plan.versions.order_by("version").values_list("price_amount", "duration_days")),
                         [(12_000, 90), (15_000, 120)])
        actions = list(AuditEvent.objects.filter(target_id=str(plan.pk)).values_list("action", flat=True))
        self.assertIn("membership.plan.created", actions)
        self.assertIn("membership.plan.updated", actions)

        page = self.client.get(plan_url(plan))
        self.assertContains(page, "TZS 15,000")
        self.assertContains(page, "120 days")
        listing = self.client.get(PLANS)
        self.assertContains(listing, "Quarterly")
        self.assertContains(listing, "TZS 15,000")
        # Members see the current price and length.
        self.client.force_login(self.member)
        detail = self.client.get(reverse("memberships:plan_detail", args=["quarterly"]))
        self.assertContains(detail, "TZS 15,000")
        self.assertContains(detail, "120 days")

    def test_invalid_prices_and_durations_are_refused(self):
        cases = {"price_amount": ("0", "-5", "12.50", "abc", "100000001"),
                 "duration_days": ("0", "-1", "3661", "1.5")}
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    response = self.client.post(CREATE, form_data(**{field: value, "code": f"bad-{field[:3]}"}))
                    self.assertEqual(response.status_code, 400)
        self.assertFalse(MembershipPlan.objects.filter(code__startswith="bad-").exists())
        for days in (1, 7, 365, 3660):  # Any duration in range, not only 30 days.
            plan = memberships.create_plan(self.root, code=f"d{days}", name=f"{days} days", price_amount=1_000,
                                           duration_days=days)
            self.assertEqual(plan.duration_days, days)
        with self.assertRaises(ValidationError):
            memberships.create_plan(self.root, code="huge", name="Huge", price_amount=100_000_001, duration_days=30)
        with override_settings(MEMBERSHIPS_MAX_PLAN_PRICE=1_000_000_000):
            self.assertEqual(memberships.create_plan(self.root, code="large", name="Large", price_amount=700_000_000,
                                                     duration_days=30).price_amount, 700_000_000)

    def test_inactive_plans_cannot_be_bought(self):
        self.client.post(plan_url(self.plan), {"action": "status", "status": "INACTIVE"})
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.status, "INACTIVE")
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("memberships:plan_detail", args=["monthly"])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("memberships:plans")), "Monthly")
        self.assertEqual(self.client.post(reverse("memberships:choose", args=["monthly"])).status_code, 404)
        with self.assertRaises(memberships.PlanUnavailable):
            memberships.choose_plan(self.member, self.plan)
        self.assertFalse(Subscription.objects.filter(member=self.member).exists())

    def test_unlisted_plan_cannot_be_bought(self):
        self.client.post(plan_url(self.plan), form_data(self.plan, is_public="", name="Monthly",
                                                        price_amount="5000", duration_days="30"))
        with self.assertRaises(memberships.PlanUnavailable):
            memberships.choose_plan(self.member, MembershipPlan.objects.get(pk=self.plan.pk))

    def test_concurrent_edit_is_detected(self):
        stale = form_data(self.plan, price_amount="6000", name="Monthly", duration_days="30")
        memberships.update_plan(self.root, self.plan, {"price_amount": 7_000})  # Someone else saves first.
        response = self.client.post(plan_url(self.plan), stale, follow=True)
        self.assertContains(response, "Another administrator changed")
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.price_amount, 7_000)

    def test_only_super_administrators_manage_plans(self):
        for user in (self.verifier, staff_with("viewer@example.com", "view_manual_payments")):
            self.client.force_login(user)
            for url in (PLANS, CREATE, plan_url(self.plan)):
                self.assertEqual(self.client.get(url).status_code, 403, url)
            self.assertEqual(self.client.post(plan_url(self.plan), {"action": "status", "status": "INACTIVE"})
                             .status_code, 403)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(PLANS).status_code, 302)  # Staff-only console.
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.status, "ACTIVE")
        with self.assertRaises(memberships.NotAuthorized):
            memberships.update_plan(self.verifier, self.plan, {"price_amount": 1})


class QuoteProtectionTests(ManualPaymentTestCase):
    def test_price_and_duration_changes_do_not_touch_pending_payments(self):
        pending = self.choose()  # Quoted TZS 5,000 for 30 days.
        claim = self.submit(pending)
        memberships.update_plan(self.root, self.plan, {"price_amount": 9_000, "duration_days": 60})
        self.client.force_login(self.member)
        # Another open choice made before the change still pays the old quote.
        other = make_user("other@example.com")
        old_choice = memberships.choose_plan(other, MembershipPlan.objects.get(pk=self.plan.pk))
        self.assertEqual((old_choice.price_amount, old_choice.duration_days), (9_000, 60))  # New choices: new terms.
        self.assertEqual(self.client.get(reverse("payments:manual_claim", args=[claim.pk])).status_code, 200)

        self.approve(claim)
        subscription = Subscription.objects.get(pk=pending.pk)
        self.assertEqual((subscription.price_amount, subscription.amount_paid, subscription.duration_days),
                         (5_000, 5_000, 30))
        self.assertEqual(subscription.expires_at - subscription.starts_at, timedelta(days=30))

    def test_pending_choice_pays_its_quote_not_the_new_price(self):
        pending = self.choose()
        memberships.update_plan(self.root, self.plan, {"price_amount": 8_000})
        with self.assertRaises(ValidationError):
            self.submit(pending, amount=8_000)
        self.client.force_login(self.member)
        page = self.client.get(reverse("payments:manual_pay", args=[pending.pk]))
        self.assertContains(page, "TZS 5,000")
        self.assertNotContains(page, "TZS 8,000")
        claim = self.submit(pending, amount=5_000)
        self.assertEqual(claim.amount, 5_000)

    def test_different_prices_and_durations_activate_correctly(self):
        for index, (price, days) in enumerate(((1_500, 1), (3_000, 7), (150_000, 365), (2_500_000, 3660))):
            member = make_user(f"buyer{index}@example.com")
            plan = self.make_plan(f"p{index}", price=price, days=days)
            claim = self.submit(memberships.choose_plan(member, plan))
            self.assertEqual(claim.amount, price)
            self.approve(claim)
            subscription = Subscription.objects.get(pk=claim.subscription_id)
            self.assertEqual((subscription.amount_paid, subscription.expires_at - subscription.starts_at),
                             (price, timedelta(days=days)))


class RenewalTests(ManualPaymentTestCase):
    def active(self):
        self.approve(self.submit())
        return Subscription.objects.get(member=self.member, status="ACTIVE")

    def at(self, moment):
        return (mock.patch("apps.memberships.services.timezone.now", return_value=moment),
                mock.patch("apps.payments.manual.timezone.now", return_value=moment))

    def test_early_renewal_extends_from_the_current_expiry(self):
        current = self.active()
        quarterly = self.make_plan("quarterly", price=12_000, days=90)
        near_end = current.expires_at - timedelta(days=5)
        first, second = self.at(near_end)
        with first, second:
            claim = self.submit(memberships.choose_plan(self.member, quarterly))
            self.approve(claim)
        renewal = Subscription.objects.get(pk=claim.subscription_id)
        self.assertEqual(renewal.starts_at, current.expires_at)
        self.assertEqual(renewal.expires_at, current.expires_at + timedelta(days=90))
        # Access is continuous across the boundary, and ends when the renewal ends.
        for moment in (current.expires_at - timedelta(seconds=1), current.expires_at,
                       renewal.expires_at - timedelta(seconds=1)):
            self.assertTrue(check_access(self.member, now=moment), moment)
        self.assertFalse(check_access(self.member, now=renewal.expires_at))

    def test_renewal_opens_only_inside_the_renewal_window(self):
        current = self.active()
        with self.assertRaises(memberships.RenewalNotAllowed) as caught:
            memberships.choose_plan(self.member, self.plan)
        self.assertEqual(caught.exception.code, "too_early")
        first, second = self.at(current.expires_at - timedelta(days=3))
        with first, second:
            self.approve(self.submit(memberships.choose_plan(self.member, self.plan)))
            with self.assertRaises(memberships.RenewalNotAllowed) as caught:  # One queued renewal at a time.
                memberships.choose_plan(self.member, self.plan)
        self.assertEqual(caught.exception.code, "already_renewed")

    def test_renewal_verified_after_expiry_starts_at_approval(self):
        current = self.active()
        first, second = self.at(current.expires_at - timedelta(days=2))
        with first, second:
            claim = self.submit(memberships.choose_plan(self.member, self.plan))
        approval = current.expires_at + timedelta(days=1)
        self.assertFalse(check_access(self.member, now=approval))  # Gap: expired, nothing granted yet.
        first, second = self.at(approval)
        with first, second:
            self.approve(claim)
        renewal = Subscription.objects.get(pk=claim.subscription_id)
        self.assertEqual((renewal.starts_at, renewal.expires_at), (approval, approval + timedelta(days=30)))

    def test_expired_member_renews_from_approval(self):
        current = self.active()
        later = current.expires_at + timedelta(days=10)
        self.assertEqual(memberships.process_expiry(now=later)["expired"], 1)
        self.assertFalse(check_access(self.member, now=later))
        first, second = self.at(later)
        with first, second:
            claim = self.submit(memberships.choose_plan(self.member, self.plan))
            self.approve(claim)
        renewal = Subscription.objects.get(pk=claim.subscription_id)
        self.assertEqual((renewal.starts_at, renewal.expires_at), (later, later + timedelta(days=30)))
        self.assertEqual(Subscription.objects.filter(member=self.member, status="ACTIVE").count(), 1)

    def test_expiry_denies_access_without_the_job(self):
        current = self.active()
        self.assertTrue(check_access(self.member, now=current.expires_at - timedelta(seconds=1)))
        self.assertFalse(check_access(self.member, now=current.expires_at))  # Job has not run.
        self.assertEqual(Subscription.objects.get(pk=current.pk).status, "ACTIVE")


@MANUAL_ON
@skipUnless(connection.vendor == "postgresql", "Concurrency is verified on PostgreSQL.")
class ConcurrentPlanAdministrationTests(TransactionTestCase):
    def setUp(self):
        from django.core.cache import cache

        SiteConfiguration.objects.update_or_create(id=1, defaults={"memberships_enabled": True})
        cache.clear()
        self.roots = [make_user(f"root{i}@example.com", role=User.Role.SUPER_ADMIN) for i in range(4)]
        self.plan = memberships.create_plan(self.roots[0], code="monthly", name="Monthly", price_amount=5_000,
                                            duration_days=30)

    def test_concurrent_edits_from_the_same_version_apply_once(self):
        results = run_threads(4, lambda i: memberships.update_plan(
            self.roots[i], self.plan, {"price_amount": 6_000 + i}, expected_version=1))
        saved = [r for r in results if isinstance(r, MembershipPlan)]
        self.assertEqual(len(saved), 1, results)
        self.assertTrue(all(getattr(r, "code", "") == "stale_edit" for r in results if r not in saved))
        self.assertEqual(MembershipPlanVersion.objects.filter(plan=self.plan).count(), 2)

    def test_choices_during_price_changes_match_a_published_version(self):
        members = [make_user(f"m{i}@example.com") for i in range(4)]

        def act(index):
            if index % 2:
                return memberships.update_plan(self.roots[index], self.plan,
                                               {"price_amount": 5_000 + index * 1_000, "duration_days": 30 + index})
            return memberships.choose_plan(members[index], self.plan)

        results = run_threads(4, act)
        self.assertFalse([r for r in results if isinstance(r, Exception)], results)
        versions = {(v.price_amount, v.duration_days, v.pk)
                    for v in MembershipPlanVersion.objects.filter(plan=self.plan)}
        for subscription in Subscription.objects.filter(plan=self.plan):
            self.assertIn((subscription.price_amount, subscription.duration_days, subscription.plan_version_id),
                          versions)
