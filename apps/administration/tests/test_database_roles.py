"""Phase 14-B (B2, audit CC-005): the health page reports whether the runtime
database login is separated from the owner of the protected tables."""

from unittest import mock

from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.helpers import User, make_user
from apps.administration import summary


class DatabaseRoleCheckTests(TestCase):
    def test_owner_login_is_reported(self):
        """The local and test databases use one owner login, so the finding must appear."""
        findings = summary.database_role_findings()
        self.assertTrue(any("protected tables" in finding for finding in findings), findings)
        self.assertTrue(any("audit events" in finding for finding in findings), findings)

    def test_health_page_shows_attention_to_super_admins_only(self):
        self.client.force_login(make_user("root@example.com", role=User.Role.SUPER_ADMIN))
        checks = {c.key: c for c in self.client.get(reverse("administration:health")).context["checks"]}
        self.assertEqual(checks["database_roles"].status, summary.ATTENTION)
        self.assertIn("CC-005", checks["database_roles"].detail)
        self.client.force_login(make_user("cm@example.com", role=User.Role.CONTENT_MANAGER))
        checks = {c.key for c in self.client.get(reverse("administration:health")).context["checks"]}
        self.assertNotIn("database_roles", checks)

    def test_separated_login_is_healthy(self):
        with mock.patch.object(summary, "database_role_findings", return_value=[]):
            self.client.force_login(make_user("root@example.com", role=User.Role.SUPER_ADMIN))
            checks = {c.key: c for c in self.client.get(reverse("administration:health")).context["checks"]}
        self.assertEqual(checks["database_roles"].status, summary.HEALTHY)
