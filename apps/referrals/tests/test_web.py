"""REF-01 pages: registration with a link or a typed code (phone-first included), invalid codes, the
member Referrals page (privacy, sharing, wallet history), the dashboard, console permissions and
actions, and English/Swahili rendering."""

from django.core.cache import cache
from django.urls import reverse
from django.utils import translation

from apps.accounts.tests.helpers import NEW_PASSWORD
from apps.referrals import services
from apps.referrals.models import Referral, ReferralPolicy
from apps.wallets.models import WalletTransaction

from .helpers import ReferralTestCase, User, staff

REGISTER = reverse("accounts:register")
OVERVIEW = reverse("referrals:overview")
PHONE = "+255712345678"


class RegistrationPageTests(ReferralTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def sign_up(self, **extra):
        data = {"first_name": "Amina", "last_name": "Juma", "contact_method": "phone", "phone_number": "0712 345 678",
                "password1": NEW_PASSWORD, "password2": NEW_PASSWORD, **extra}
        return self.client.post(REGISTER, data)

    def test_link_attribution_survives_to_phone_first_registration(self):
        response = self.client.get(reverse("referrals:join", args=[self.code.code]))
        self.assertRedirects(response, REGISTER)
        page = self.client.get(REGISTER)
        self.assertContains(page, f'value="{self.code.code}"')
        response = self.sign_up(referral_code=self.code.code)
        self.assertRedirects(response, reverse("dashboard"))
        referral = User.objects.get(phone_number=PHONE).referral
        self.assertEqual((referral.referrer, referral.source), (self.member, "LINK"))
        self.assertNotIn(services.SESSION_KEY, self.client.session)

    def test_query_string_link_and_manual_code(self):
        self.client.get(REGISTER + f"?ref={self.code.code}")
        self.assertEqual(self.client.session[services.SESSION_KEY], self.code.code)
        other = self.client_class()
        response = other.post(REGISTER, {"first_name": "Juma", "last_name": "Ali", "contact_method": "phone",
                                         "phone_number": "0713 000 111", "password1": NEW_PASSWORD,
                                         "password2": NEW_PASSWORD, "referral_code": self.code.code.lower()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(User.objects.get(phone_number="+255713000111").referral.source, "CODE")

    def test_invalid_code_is_explained_and_nothing_is_created(self):
        response = self.sign_up(referral_code="NOTACODE")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This referral code is not valid")
        self.assertFalse(User.objects.filter(phone_number=PHONE).exists())

    def test_without_a_code_and_when_paused(self):
        self.assertRedirects(self.sign_up(), reverse("dashboard"))
        self.assertFalse(Referral.objects.exists())
        self.rules(tracking_open=False)
        self.assertNotContains(self.client_class().get(REGISTER), 'name="referral_code"')

    def test_signed_in_members_cannot_use_links(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("referrals:join", args=[self.code.code]))
        self.assertRedirects(response, reverse("dashboard"))


class MemberPageTests(ReferralTestCase):
    def setUp(self):
        super().setUp()
        self.rules()
        self.fund(5_000)
        self.client.force_login(self.member)

    def test_page_shows_real_figures_without_private_details(self):
        user = self.join(first="Neema", last="Mushi", email="neema.private@x.example")
        User.objects.filter(pk=user.pk).update(phone_number="+255754111222")
        self.qualify(user)
        self.client.force_login(self.member)  # The test clock moved past the idle-session limit.
        page = self.client.get(OVERVIEW)
        self.assertContains(page, self.code.code)
        self.assertContains(page, reverse("referrals:join", args=[self.code.code]))
        self.assertContains(page, "https://wa.me/?text=")
        self.assertContains(page, "data-native-share")
        self.assertContains(page, "N••• M.")
        for secret in ("neema.private@x.example", "754111222", "Mushi"):
            self.assertNotContains(page, secret)
        self.assertContains(page, "TZS 1,000")
        wallet = self.client.get(reverse("wallets:wallet"))
        self.assertContains(wallet, "Referral reward")
        self.assertTrue(WalletTransaction.objects.filter(category="REFERRAL_CREDIT").exists())

    def test_dashboard_is_read_only_and_links_to_referrals(self):
        codes = list(Referral.objects.values_list("pk", flat=True))
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, OVERVIEW)
        self.assertContains(page, self.code.code)
        self.assertEqual(list(Referral.objects.values_list("pk", flat=True)), codes)

    def test_staff_accounts_have_no_referral_code(self):
        self.client.force_login(self.reviewer)
        page = self.client.get(OVERVIEW)
        self.assertNotContains(page, "data-copy-value")

    def test_swahili(self):
        self.client.cookies["django_language"] = "sw"
        with translation.override("sw"):
            page = self.client.get(OVERVIEW, HTTP_ACCEPT_LANGUAGE="sw")
        self.assertContains(page, "Mialiko")
        self.assertNotContains(page, "Total referral earnings")


class ConsoleTests(ReferralTestCase):
    def setUp(self):
        super().setUp()
        self.rules()
        self.fund(5_000)

    def test_permissions(self):
        for url in (reverse("administration:referrals"), reverse("administration:referral_programme"),
                    reverse("administration:referral_funding")):
            self.client.force_login(staff("plain@x.example") if not User.objects.filter(
                email="plain@x.example").exists() else User.objects.get(email="plain@x.example"))
            self.assertEqual(self.client.get(url).status_code, 403)
            self.client.force_login(self.reviewer)
            self.assertEqual(self.client.get(url).status_code, 200)
            self.client.force_login(self.member)
            self.assertNotEqual(self.client.get(url).status_code, 200)

    def test_review_and_policy_actions_through_the_console(self):
        user = self.join()
        self.destination(member=self.member, number="0754999888", age_hours=0)
        self.destination(member=user, number="0754999888", age_hours=0)
        self.qualify(user)
        referral = self.referral_for(user)
        self.client.force_login(self.reviewer)
        detail = reverse("administration:referral", args=[referral.pk])
        page = self.client.get(detail)
        self.assertContains(page, "Same payout destination as the referrer")
        self.assertNotContains(page, 'name="amount"')  # Reviewers can never choose an amount.
        self.client.post(detail, {"action": "approve", "note": "Family, checked by phone"})
        referral.refresh_from_db()
        self.assertEqual(referral.status, Referral.Status.REWARD_PENDING)
        # Policy: proposed by one person, approved by another.
        self.client.force_login(self.proposer)
        url = reverse("administration:referral_programme")
        data = {"action": "propose", "expected_version": 2, "reason": "Lower reward", "tracking_open": "on",
                "rewards_open": "on", "reward_amount": "800", "referrer_requires_membership": "on",
                "min_account_age_days": 1, "required_verified_views": 2, "required_view_days": 2,
                "qualification_days": 30, "daily_reward_limit": 5, "lifetime_reward_limit": 10, "clearance_hours": 24}
        self.client.post(url, data)
        proposal = ReferralPolicy.objects.get(status="PROPOSED")
        self.client.post(url, {"action": "approve", "proposal": proposal.pk})
        self.assertEqual(services.current_policy().version, 2)  # The proposer cannot approve.
        self.client.force_login(self.policy_approver)
        self.client.post(url, {"action": "approve", "proposal": proposal.pk})
        self.assertEqual(services.current_policy().reward_amount, 800)

    def test_funding_page_four_eyes(self):
        self.client.force_login(self.mk_recorder)
        url = reverse("administration:referral_funding")
        self.client.post(url, {"action": "record", "method": "BANK_TRANSFER", "amount": "2500",
                               "external_reference": "BANKREF2026", "paid_on": self.clock.now.date().isoformat()})
        page = self.client.get(url)
        self.assertContains(page, "BANKREF2026")
        self.assertNotContains(page, 'name="receipt_confirmed"')  # The recorder cannot verify it.
        from apps.referrals.models import MarketingDeposit

        deposit = MarketingDeposit.objects.get(external_reference="BANKREF2026")
        self.client.force_login(self.mk_verifier)
        self.client.post(url, {"action": "verify", "deposit": deposit.pk, "receipt_confirmed": "on"})
        deposit.refresh_from_db()
        self.assertEqual(deposit.status, "VERIFIED")
