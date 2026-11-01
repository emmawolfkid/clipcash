from django.contrib.auth.models import Permission
from django.urls import reverse

from .helpers import PASSWORD, LedgerTestCase, User, postings_enabled

LIST_URLS = [reverse(f"admin:wallets_{name}_changelist") for name in ("ledgeraccount", "journalentry", "journalline")]


class LedgerAdminTests(LedgerTestCase):
    def setUp(self):
        super().setUp()
        with postings_enabled():
            self.receipt = self.receive(1_200, reference="PROV-ADMIN-1")
        self.cm = User.objects.create_user("cm@example.com", PASSWORD, role=User.Role.CONTENT_MANAGER,
                                           status=User.Status.ACTIVE)

    def test_super_admin_can_view_read_only(self):
        self.client.force_login(self.root)
        for url in LIST_URLS:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.context["action_form"])
        detail = self.client.get(reverse("admin:wallets_journalentry_change", args=[self.receipt.pk]))
        self.assertContains(detail, "TZS 1,200")
        self.assertFalse(detail.context["has_change_permission"])

    def test_staff_without_explicit_permission_refused(self):
        self.client.force_login(self.cm)
        for url in LIST_URLS:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
        self.assertNotContains(self.client.get(reverse("admin:index")), "Journal entries")

    def test_granted_staff_view_only_no_writes(self):
        self.cm.user_permissions.add(*Permission.objects.filter(content_type__app_label="wallets"))
        self.client.force_login(self.cm)
        self.assertEqual(self.client.get(LIST_URLS[1]).status_code, 200)
        for name in ("ledgeraccount", "journalentry", "journalline"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(f"admin:wallets_{name}_add")).status_code, 403)
        change_url = reverse("admin:wallets_journalentry_change", args=[self.receipt.pk])
        self.assertEqual(self.client.post(change_url, {"description": "edited"}).status_code, 403)
        delete_url = reverse("admin:wallets_journalentry_delete", args=[self.receipt.pk])
        self.assertEqual(self.client.post(delete_url, {"post": "yes"}).status_code, 403)

    def test_members_and_anonymous_refused(self):
        self.assertEqual(self.client.get(LIST_URLS[0]).status_code, 302)

    def test_filters_and_search(self):
        self.client.force_login(self.root)
        cases = {
            # Campaign funding, member reward liability and platform withdrawal clearing.
            f"{LIST_URLS[0]}?funding_class__exact=REWARD_RESTRICTED": 3,
            f"{LIST_URLS[0]}?q=payment_clearing": 1,
            f"{LIST_URLS[1]}?q=PROV-ADMIN-1": 1,
            f"{LIST_URLS[1]}?posting_type__exact=advertiser.funding_received": 1,
            f"{LIST_URLS[1]}?reversal=reversal": 0,
            f"{LIST_URLS[2]}?side__exact=DEBIT": 1,
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.context["cl"].result_list), expected)
