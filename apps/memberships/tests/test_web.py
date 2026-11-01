from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.memberships import services
from apps.memberships.models import MembershipPlan, MembershipPlanVersion, Subscription

from .helpers import PASSWORD, MembershipTestCase, User, enable_memberships, make_user

PLANS = reverse("memberships:plans")
MINE = reverse("memberships:mine")


class MemberPagesTests(MembershipTestCase):
    def test_public_plan_list_and_detail(self):
        hidden = self.make_plan("staff-only", is_public=False)
        response = self.client.get(PLANS)
        self.assertContains(response, "Monthly")
        self.assertContains(response, "TZS 5,000")
        self.assertNotContains(response, hidden.name)
        detail = self.client.get(reverse("memberships:plan_detail", args=["monthly"]))
        self.assertContains(detail, "Online payment is not available yet")
        self.assertContains(detail, "not an investment")
        for misleading in ("Pay now", "Pay Now", "Checkout"):
            self.assertNotContains(detail, misleading)
        self.assertEqual(self.client.get(reverse("memberships:plan_detail", args=["staff-only"])).status_code, 404)

    def test_pages_hidden_when_feature_off(self):
        enable_memberships(False)
        self.client.force_login(self.member)
        for url in (PLANS, MINE, reverse("memberships:plan_detail", args=["monthly"])):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_choosing_requires_verified_sign_in_and_post(self):
        choose = reverse("memberships:choose", args=["monthly"])
        self.assertEqual(self.client.post(choose).status_code, 302)  # To login.
        self.assertFalse(Subscription.objects.exists())
        self.client.force_login(make_user("unverified@example.com", verified=False))
        self.assertRedirects(self.client.post(choose), reverse("accounts:verification_required"))
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(choose).status_code, 405)
        self.assertRedirects(self.client.post(choose), MINE)
        page = self.client.get(MINE)
        self.assertContains(page, "Waiting for payment")
        self.assertContains(page, "Online payment is not available yet")

    def test_members_see_only_their_own_records(self):
        mine = services.choose_plan(self.member, self.plan)
        other = make_user("other@example.com")
        theirs = services.choose_plan(other, self.plan)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("memberships:subscription_detail", args=[mine.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("memberships:subscription_detail", args=[theirs.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("memberships:cancel_pending", args=[theirs.pk])).status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, Subscription.Status.PENDING)
        self.assertNotContains(self.client.get(MINE), "other@example.com")

    def test_anonymous_cannot_see_private_pages(self):
        pending = services.choose_plan(self.member, self.plan)
        for url in (MINE, reverse("memberships:subscription_detail", args=[pending.pk])):
            with self.subTest(url=url):
                self.assertIn(reverse("accounts:login"), self.client.get(url)["Location"])

    def test_active_membership_and_history_shown(self):
        self.active_membership()
        self.client.force_login(self.member)
        page = self.client.get(MINE)
        self.assertContains(page, "Active")
        self.assertContains(page, "Renewal opens on")


class MembershipAdminTests(MembershipTestCase):
    client_class = Client  # No worker side effects needed here.

    def plan_form(self, **overrides):
        data = {"code": "weekly", "name": "Weekly", "description": "", "price_amount": "1500",
                "duration_days": "7", "display_order": "10", "is_public": "on",
                "benefits_text": "Standard library\nNo ads at night", "entitlement_codes": ["videos.standard"],
                "versions-TOTAL_FORMS": "0", "versions-INITIAL_FORMS": "0", "_save": "Save"}
        data.update(overrides)
        return data

    def test_super_admin_creates_and_edits_plan_with_versioning(self):
        self.client.force_login(self.root)
        response = self.client.post(reverse("admin:memberships_membershipplan_add"), self.plan_form())
        self.assertEqual(response.status_code, 302, response.content[:2000])
        plan = MembershipPlan.objects.get(code="weekly")
        self.assertEqual((plan.benefits, plan.entitlements), (["Standard library", "No ads at night"],
                                                              ["videos.standard"]))
        pending = services.choose_plan(self.member, plan)

        change = reverse("admin:memberships_membershipplan_change", args=[plan.pk])
        data = self.plan_form(price_amount="2000")
        data.pop("code")
        data.update({"versions-TOTAL_FORMS": "1", "versions-INITIAL_FORMS": "1",
                     "versions-0-id": str(plan.versions.get().pk), "versions-0-plan": str(plan.pk)})
        self.assertEqual(self.client.post(change, data).status_code, 302)
        plan.refresh_from_db()
        self.assertEqual((plan.price_amount, plan.version, plan.code), (2_000, 2, "weekly"))
        self.assertEqual(MembershipPlanVersion.objects.filter(plan=plan).count(), 2)
        pending.refresh_from_db()
        self.assertEqual(pending.price_amount, 1_500)  # Historical price unchanged.
        self.assertTrue(AuditEvent.objects.filter(action="membership.plan.updated", actor_id=self.root.pk).exists())

    def test_non_super_admins_refused_even_with_permissions(self):
        cm = make_user("cm@example.com", role=User.Role.CONTENT_MANAGER)
        cm.user_permissions.add(*Permission.objects.filter(content_type__app_label="memberships",
                                                           codename__contains="plan"))
        self.client.force_login(cm)
        self.assertEqual(self.client.get(reverse("admin:memberships_membershipplan_changelist")).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:memberships_membershipplan_add"),
                                          self.plan_form()).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:memberships_subscription_changelist")).status_code, 403)
        self.assertFalse(MembershipPlan.objects.filter(code="weekly").exists())

    def test_no_plan_deletion_and_subscriptions_read_only(self):
        subscription = self.active_membership()
        self.client.force_login(self.root)
        self.assertEqual(self.client.post(reverse("admin:memberships_membershipplan_delete", args=[self.plan.pk]),
                                          {"post": "yes"}).status_code, 403)
        detail = self.client.get(reverse("admin:memberships_subscription_change", args=[subscription.pk]))
        self.assertFalse(detail.context["has_change_permission"])
        self.assertEqual(self.client.get(reverse("admin:memberships_subscription_add")).status_code, 403)
        actions = [choice[0] for choice in self.client.get(reverse("admin:memberships_subscription_changelist"))
                   .context["action_form"].fields["action"].choices]
        self.assertNotIn("activate", " ".join(actions))
        self.assertNotIn("delete_selected", actions)

    def test_suspend_action_requires_reason_and_permission(self):
        subscription = self.active_membership()
        url = reverse("admin:memberships_subscription_changelist")
        viewer = make_user("viewer@example.com", role=User.Role.CONTENT_MANAGER)
        viewer.user_permissions.add(Permission.objects.get(codename="view_memberships"))
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertIsNone(self.client.get(url).context["action_form"])  # Viewing only.

        self.client.force_login(self.root)
        confirm = self.client.post(url, {"action": "suspend_selected", "_selected_action": [str(subscription.pk)]})
        self.assertContains(confirm, "Suspend memberships")
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Subscription.Status.ACTIVE)  # Nothing until confirmed.
        self.client.post(url, {"action": "suspend_selected", "_selected_action": [str(subscription.pk)],
                               "apply": "1", "reason": "POLICY_VIOLATION"})
        subscription.refresh_from_db()
        self.assertEqual((subscription.status, subscription.suspension_reason),
                         (Subscription.Status.SUSPENDED, "POLICY_VIOLATION"))

    def test_expiring_soon_filter(self):
        self.active_membership()
        self.client.force_login(self.root)
        url = reverse("admin:memberships_subscription_changelist")
        self.assertEqual(len(self.client.get(f"{url}?expiring=soon").context["cl"].result_list), 0)
        self.assertEqual(len(self.client.get(f"{url}?status__exact=ACTIVE").context["cl"].result_list), 1)


class SignInFlowTests(MembershipTestCase):
    def test_signed_in_member_reaches_plans_and_membership_pages(self):
        self.client.post(reverse("accounts:login"), {"username": "member@example.com", "password": PASSWORD})
        self.assertContains(self.client.get(reverse("memberships:plan_detail", args=["monthly"])), "Choose this plan")
        self.assertEqual(self.client.get(MINE).status_code, 200)
