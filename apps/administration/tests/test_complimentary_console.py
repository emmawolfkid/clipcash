"""Phase 14-A: console workflow for time-limited complimentary demo memberships."""

import zoneinfo
from datetime import timedelta

from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.core.services import get_site_configuration
from apps.memberships.models import Subscription
from apps.memberships.tests.helpers import MembershipTestCase, User, make_user
from apps.memberships.tests.test_complimentary import GRANTS, staff

FORMAT = "%Y-%m-%dT%H:%M"


@override_settings(**GRANTS)
class ComplimentaryConsoleTests(MembershipTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("administration:member_complimentary", args=[self.member.pk])
        self.detail = reverse("administration:member", args=[self.member.pk])
        # The console reads the entered time in the site time zone (SiteConfigurationMiddleware).
        self.site_zone = zoneinfo.ZoneInfo(get_site_configuration().default_timezone)
        self.expiry = (timezone.now() + timedelta(days=3)).astimezone(self.site_zone).replace(second=0, microsecond=0)

    def values(self, **extra):
        return {"plan": self.plan.pk, "expires_at": self.expiry.strftime(FORMAT),
                "reason": "Client demonstration", "operation": "token-0001", **extra}

    def confirm(self, client=None, **extra):
        return (client or self.client).post(self.url, self.values(step="confirm", confirm="on", **extra))

    def test_super_admin_reviews_then_confirms(self):
        self.client.force_login(self.root)
        detail = self.client.get(self.detail)
        self.assertContains(detail, self.url)
        page = self.client.get(self.url)
        self.assertContains(page, "It is not a payment")
        self.assertContains(page, '<label for="id_reason"')
        self.assertContains(page, 'type="datetime-local"')

        review = self.client.post(self.url, self.values(step="review"))
        self.assertContains(review, "Review and confirm")
        self.assertContains(review, "TZS 0")
        self.assertFalse(Subscription.objects.exists())  # Review changes nothing.

        response = self.confirm()
        self.assertRedirects(response, self.detail)
        granted = Subscription.objects.get(member=self.member)
        self.assertEqual((granted.status, granted.activation_source, granted.expires_at, granted.note),
                         ("ACTIVE", "COMPLIMENTARY", self.expiry, "Client demonstration"))
        detail = self.client.get(self.detail)
        self.assertContains(detail, "Complimentary (no payment)")

    def test_confirmation_box_is_required(self):
        self.client.force_login(self.root)
        response = self.client.post(self.url, self.values(step="confirm"))
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Tick the confirmation box", status_code=400)
        self.assertFalse(Subscription.objects.exists())

    def test_validation_errors_are_shown(self):
        self.client.force_login(self.root)
        past = (timezone.now() - timedelta(hours=1)).astimezone(self.site_zone).strftime(FORMAT)
        response = self.confirm(expires_at=past)
        self.assertContains(response, "The expiry must be in the future.", status_code=400)
        response = self.client.post(self.url, self.values(step="review", reason="no"))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Subscription.objects.exists())

    def test_existing_membership_message(self):
        self.active_membership()
        self.client.force_login(self.root)
        self.assertContains(self.client.get(self.url), "never replaced")
        response = self.confirm()
        self.assertContains(response, "already has a current or scheduled membership", status_code=400)
        self.assertEqual(Subscription.objects.get(member=self.member).activation_source, "PAYMENT")

    def test_resubmitting_the_same_form_does_not_grant_twice(self):
        self.client.force_login(self.root)
        self.confirm()
        self.assertRedirects(self.confirm(), self.detail)
        self.assertEqual(Subscription.objects.filter(member=self.member).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="membership.complimentary.granted").count(), 1)

    def test_resubmitting_after_cancellation_reports_the_real_state(self):
        from apps.memberships import services

        self.client.force_login(self.root)
        self.assertContains(self.client.post(self.url, self.values(step="confirm", confirm="on"), follow=True),
                            "granted until")  # The browser follows the redirect and shows the message once.
        services.cancel(self.root, Subscription.objects.get(member=self.member), "ADMIN_REVIEW")
        response = self.confirm()  # Same form, e.g. Back button and resubmit.
        self.assertContains(response, "no longer active (cancelled)", status_code=400)
        self.assertNotContains(response, "granted until", status_code=400)
        self.assertEqual(Subscription.objects.get(member=self.member).status, "CANCELLED")
        self.assertEqual(AuditEvent.objects.filter(action="membership.complimentary.granted").count(), 1)

    def test_resubmitting_with_changed_inputs_is_a_conflict(self):
        self.client.force_login(self.root)
        self.confirm()
        response = self.confirm(reason="Edited after granting")
        self.assertContains(response, "already used for a different grant", status_code=400)
        self.assertEqual(Subscription.objects.get(member=self.member).note, "Client demonstration")

    def test_unauthorized_users_are_rejected(self):
        callers = (staff("cm@example.com"), staff("fin@example.com", "payments.view_payments", "wallets.view_wallets"),
                   staff("sup@example.com", "accounts.view_user"), staff("aud@example.com", "audit.view_auditevent"))
        for caller in callers:
            self.client.force_login(caller)
            with self.subTest(caller=caller.email):
                self.assertEqual(self.client.get(self.url).status_code, 403)
                self.assertEqual(self.confirm().status_code, 403)
            if caller.has_perm("accounts.view_user"):
                self.assertNotContains(self.client.get(self.detail), self.url)
        login = f"{reverse('admin:login')}?next={self.url}"
        self.client.force_login(self.member)  # A member cannot reach the console, even for themselves.
        self.assertRedirects(self.confirm(), login, fetch_redirect_response=False)
        self.client.logout()
        self.assertRedirects(self.confirm(), login, fetch_redirect_response=False)
        self.assertFalse(Subscription.objects.exists())

    def test_hidden_when_grants_are_not_enabled(self):
        self.client.force_login(self.root)
        with override_settings(RESTRICTED_DEMO_MODE=False):
            self.assertNotContains(self.client.get(self.detail), self.url)
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.assertEqual(self.confirm().status_code, 403)
        self.assertFalse(Subscription.objects.exists())

    def test_only_member_accounts_can_be_targeted(self):
        self.client.force_login(self.root)
        other_staff = make_user("cm2@example.com", role=User.Role.CONTENT_MANAGER)
        url = reverse("administration:member_complimentary", args=[other_staff.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(reverse("administration:member_complimentary",
                                                 args=[self.root.pk])).status_code, 404)

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.root)
        self.assertEqual(self.confirm(client).status_code, 403)
        self.assertFalse(Subscription.objects.exists())

    def test_member_pages_show_the_grant_as_complimentary(self):
        self.client.force_login(self.root)
        self.confirm()
        self.client.force_login(self.member)
        dashboard = self.client.get(reverse("dashboard"))
        self.assertContains(dashboard, "Complimentary (no payment)")
        self.assertContains(dashboard, self.plan.name)
        mine = self.client.get(reverse("memberships:mine"))
        self.assertContains(mine, "Complimentary demonstration access: no payment was made")
