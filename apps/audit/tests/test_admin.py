from django.contrib.auth.models import Permission
from django.db import transaction
from django.urls import reverse

from apps.accounts.tests.helpers import AccountsTestCase, User, make_user
from apps.audit import services
from apps.audit.models import AuditEvent

LIST_URL = reverse("admin:audit_auditevent_changelist")


class AuditWorkspaceTests(AccountsTestCase):
    def setUp(self):
        super().setUp()
        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.cm = make_user("cm@example.com", role=User.Role.CONTENT_MANAGER)
        with transaction.atomic():
            self.event = services.record_business_event(
                "account.status_changed", actor=self.root, target=self.cm,
                metadata={"from_status": "ACTIVE", "to_status": "SUSPENDED"},
            )
        services.record_security_event("auth.login.failed", outcome=services.FAILURE, actor=None,
                                       metadata={"identifier_hash": "abc", "account_exists": False})

    def detail_url(self, event):
        return reverse("admin:audit_auditevent_change", args=[event.pk])

    def test_super_admin_can_browse_and_view(self):
        self.client.force_login(self.root)
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "account.status_changed")
        detail = self.client.get(self.detail_url(self.event))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "SUSPENDED")
        self.assertFalse(detail.context["has_change_permission"])

    def test_staff_without_explicit_permission_refused(self):
        self.client.force_login(self.cm)
        self.assertEqual(self.client.get(LIST_URL).status_code, 403)
        self.assertEqual(self.client.get(self.detail_url(self.event)).status_code, 403)
        self.assertNotContains(self.client.get(reverse("admin:index")), "Audit events")

    def test_explicitly_granted_staff_can_view_only(self):
        self.cm.user_permissions.add(*Permission.objects.filter(content_type__app_label="audit"))
        self.client.force_login(self.cm)
        self.assertEqual(self.client.get(LIST_URL).status_code, 200)
        self.assertEqual(self.client.post(self.detail_url(self.event), {"outcome": "FAILURE"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:audit_auditevent_add")).status_code, 403)
        delete_url = reverse("admin:audit_auditevent_delete", args=[self.event.pk])
        self.assertEqual(self.client.post(delete_url, {"post": "yes"}).status_code, 403)
        self.assertTrue(AuditEvent.objects.filter(pk=self.event.pk, outcome="SUCCESS").exists())

    def test_members_and_anonymous_refused(self):
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 302)
        member = make_user()
        self.client.force_login(member)
        self.assertEqual(self.client.get(LIST_URL).status_code, 302)

    def test_filters_search_and_pagination(self):
        self.client.force_login(self.root)
        queries = {
            "outcome=FAILURE": "auth.login.failed",
            "action=account.status_changed": "account.status_changed",
            "category__exact=ACCOUNT": "account.status_changed",
            f"actor_id={self.root.pk}": "account.status_changed",
            f"target_id={self.cm.pk}": "account.status_changed",
            "q=cm@example.com": "account.status_changed",
            f"q={self.event.request_id or 'nomatch'}": None,
            "occurred_at__year=2000": None,
        }
        for query, expected in queries.items():
            with self.subTest(query=query):
                response = self.client.get(f"{LIST_URL}?{query}")
                self.assertEqual(response.status_code, 200)
                results = [e.action for e in response.context["cl"].result_list]
                if expected:
                    self.assertEqual(results, [expected])
        self.assertEqual(self.client.get(f"{LIST_URL}?p=1").status_code, 200)

    def test_invalid_filter_value_handled(self):
        self.client.force_login(self.root)
        response = self.client.get(f"{LIST_URL}?actor_id=not-a-uuid")
        self.assertIn(response.status_code, (200, 302))

    def test_no_bulk_actions(self):
        self.client.force_login(self.root)
        self.assertIsNone(self.client.get(LIST_URL).context["action_form"])
