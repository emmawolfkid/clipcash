"""
PRD-01 + PRD-03A acceptance: registration, sign-in and recovery, with fictional test records only.

PRD-03A: members sign up with an email address, a Tanzanian mobile number OR a username and are
signed in at once. Nothing is emailed at sign-up and nothing has to be confirmed to use the
account; the identifier is stored unconfirmed. Confirming an email address stays available on
request and is needed to buy a membership. A password reset goes to a confirmed address, or to
the address an email sign-up uses as its only identifier; completing it confirms that address.
Mobile numbers and usernames never reset a password. Existing accounts, memberships and
submitted payments keep working.
"""

import re

from django.contrib.auth import get_user
from django.core import mail
from django.core.checks import run_checks
from django.test import override_settings
from django.urls import reverse

from apps.accounts.access import has_verified_contact
from apps.accounts.tests.helpers import NEW_PASSWORD, PASSWORD, make_user
from apps.audit.models import AuditEvent
from apps.memberships import services as memberships
from apps.memberships.models import Subscription
from apps.notifications.models import OutboundEmail
from apps.payments.models import ManualMembershipPayment
from apps.payments.tests.manual_helpers import ManualPaymentTestCase, User

REGISTER = reverse("accounts:register")
LOGIN = reverse("accounts:login")
DASHBOARD = reverse("dashboard")
PROFILE = reverse("accounts:profile")
RESET = reverse("accounts:password_reset")
RESEND = reverse("accounts:resend_verification")
EMAIL_CHANGE = reverse("accounts:email_change")
PROFILE_EDIT = reverse("accounts:profile_edit")
PASSWORD_CHANGE = reverse("accounts:password_change")


def sign_up(client, *, email=None, phone=None, username=None, password=NEW_PASSWORD):
    method = "email" if email else "phone" if phone else "username"
    data = {"first_name": "Amina", "last_name": "Juma", "password1": password, "password2": password,
            "contact_method": method, "email": email or "", "phone_number": phone or "", "username": username or ""}
    return client.post(REGISTER, data)


def links(address, fragment):
    found = []
    for message in mail.outbox:
        if address in message.to:
            found += [link for link in re.findall(r"https?://\S+", message.body) if fragment in link]
    return found


def confirmation_link(address):
    found = links(address, "/accounts/confirm-email/")
    return found[-1] if found else None


def path_of(link):
    return "/" + link.split("://", 1)[1].split("/", 1)[1]


def subjects(address):
    return [message.subject for message in mail.outbox if address in message.to]


class InstantSignUpTests(ManualPaymentTestCase):
    """ManualPaymentTestCase: registration open, a 5,000 TZS plan, published Lipa details,
    manual payments on, and a client that delivers queued email after each request."""

    def test_the_form_offers_email_phone_or_username(self):
        page = self.client.get(REGISTER)
        self.assertEqual(list(page.context["form"].fields),
                         ["first_name", "last_name", "contact_method", "email", "phone_number", "username",
                          "password1", "password2", "referral_code"])  # REF-01: optional code.
        self.assertContains(page, "Your account is ready at once")

    def test_every_method_is_signed_in_at_once_and_nothing_is_emailed(self):
        cases = {"email": {"email": "Amina@Example.com"}, "phone": {"phone": "0712 345 678"},
                 "username": {"username": "Amina.Juma"}}
        for method, identifier in cases.items():
            with self.subTest(method):
                client = self.client_class()
                self.assertRedirects(sign_up(client, **identifier), DASHBOARD, fetch_redirect_response=False)
                member = get_user(client)
                self.assertTrue(member.is_authenticated)
                self.assertEqual((member.status, member.email_verified, member.phone_verified),
                                 (User.Status.ACTIVE, False, False))  # Stored unconfirmed, never marked verified.
                for url in (DASHBOARD, PROFILE, reverse("memberships:plans")):
                    self.assertEqual(client.get(url).status_code, 200, url)
        self.assertIsNone(User.objects.get(username="amina.juma").email)
        self.assertIsNone(User.objects.get(email="amina@example.com").username)
        self.assertFalse(OutboundEmail.objects.exists())  # No sign-up verification email, for any method.
        self.assertEqual(mail.outbox, [])
        methods = sorted(event.metadata["method"] for event in AuditEvent.objects.filter(action="account.registered"))
        self.assertEqual(methods, ["email", "phone", "username"])

    def test_each_identifier_signs_in_again_later(self):
        sign_up(self.client_class(), email="amina@example.com")
        sign_up(self.client_class(), username="baraka")
        for identifier in ("AMINA@example.com", "Baraka"):
            with self.subTest(identifier):
                client = self.client_class()
                client.post(LOGIN, {"username": identifier, "password": NEW_PASSWORD})
                self.assertTrue(get_user(client).is_authenticated)

    def test_identifiers_stay_unique_in_any_format_or_case(self):
        sign_up(self.client_class(), email="amina@example.com")
        sign_up(self.client_class(), phone="0712345678")
        sign_up(self.client_class(), username="baraka")
        cases = (({"email": "AMINA@Example.com"}, "email"), ({"phone": "+255 712 345 678"}, "phone_number"),
                 ({"username": "BARAKA"}, "username"))
        for identifier, field in cases:
            with self.subTest(field):
                refused = sign_up(self.client_class(), **identifier)
                self.assertEqual(refused.status_code, 200)
                self.assertIn(field, refused.context["form"].errors)
        self.assertEqual(User.objects.filter(first_name="Amina").count(), 3)
        self.assertEqual(AuditEvent.objects.filter(action="account.registration.duplicate").count(), 3)

    def test_usernames_keep_their_rules(self):
        for value in ("0712345678", "amina@example.com", "am", "admin"):
            with self.subTest(value=value):
                refused = sign_up(self.client_class(), username=value)
                self.assertIn("username", refused.context["form"].errors)
        self.assertFalse(User.objects.filter(first_name="Amina").exists())

    def test_sign_up_is_rate_limited_per_identifier(self):
        for _attempt in range(5):
            sign_up(self.client_class(), email="busy@example.com")
        refused = sign_up(self.client_class(), email="busy@example.com")
        self.assertContains(refused, "Too many requests")

    def test_confirming_an_email_is_optional_on_request_and_needed_to_pay(self):
        sign_up(self.client, email="amina@example.com")
        member = User.objects.get(email="amina@example.com")
        with self.assertRaises(memberships.NotEligible):
            memberships.choose_plan(member, self.plan)
        detail = self.client.get(reverse("memberships:plan_detail", args=[self.plan.code]))
        self.assertContains(detail, "Confirm your email address to buy a membership")
        self.assertNotContains(self.client.get(DASHBOARD), "Confirm your email address")  # No banner.
        self.assertRedirects(self.client.post(RESEND), PROFILE)
        link = path_of(confirmation_link("amina@example.com"))
        self.assertEqual(self.client_class().get(link).status_code, 403)  # Only the signed-in owner confirms.
        self.client.post(link)
        member.refresh_from_db()
        self.assertTrue(member.email_verified)
        self.assertEqual(memberships.choose_plan(member, self.plan).status, Subscription.Status.PENDING)

    def test_reset_by_the_sign_up_address_works_once_and_confirms_it(self):
        sign_up(self.client_class(), email="amina@example.com")
        mail.outbox.clear()
        self.client_class().post(RESET, {"email": "amina@example.com"})
        reset = path_of(links("amina@example.com", "/accounts/reset/")[-1])
        resetter = self.client_class()
        form_page = resetter.get(reset, follow=True)
        resetter.post(form_page.request["PATH_INFO"], {"new_password1": "Third-Strong-Phrase-5",
                                                      "new_password2": "Third-Strong-Phrase-5"})
        member = User.objects.get(email="amina@example.com")
        self.assertTrue(member.check_password("Third-Strong-Phrase-5"))
        self.assertTrue(member.email_verified)  # The link reached this inbox.
        self.assertIn("Your ClipCash password was changed", subjects("amina@example.com"))
        self.assertFalse(resetter.get(reset, follow=True).context.get("validlink", False))  # Single use.

    def test_phone_and_username_never_reset_and_unknown_addresses_look_the_same(self):
        sign_up(self.client_class(), phone="0712345678")
        sign_up(self.client_class(), username="baraka")
        mail.outbox.clear()
        for typed in ("0712345678", "baraka", "nobody@example.com"):
            with self.subTest(typed):
                self.client_class().post(RESET, {"email": typed})
        self.assertEqual(mail.outbox, [])
        self.assertRedirects(self.client_class().post(RESET, {"email": "nobody@example.com"}),
                             reverse("accounts:password_reset_done"))

    def test_prd01_pending_sign_ups_still_activate_through_their_link(self):
        pending = User.objects.create_user("waiting@example.com", NEW_PASSWORD, status=User.Status.PENDING,
                                           email_ownership_required=True)
        self.client_class().post(RESEND, {"email": "waiting@example.com"})
        self.assertEqual(self.client_class().post(path_of(confirmation_link("waiting@example.com"))).status_code, 200)
        pending.refresh_from_db()
        self.assertEqual((pending.status, pending.email_verified), (User.Status.ACTIVE, True))


class PhoneSignUpTests(ManualPaymentTestCase):
    def test_phone_sign_up_is_active_and_signed_in_but_unverified(self):
        self.assertRedirects(sign_up(self.client, phone="0712 345 678"), DASHBOARD, fetch_redirect_response=False)
        member = User.objects.get(phone_number="+255712345678")
        self.assertEqual((member.status, member.phone_verified, member.email, member.username),
                         (User.Status.ACTIVE, False, None, None))
        self.assertEqual(get_user(self.client).pk, member.pk)
        self.assertFalse(has_verified_contact(member))
        self.assertEqual(mail.outbox, [])
        self.assertEqual(self.client.get(DASHBOARD).status_code, 200)

    def test_number_signs_in_in_any_format(self):
        sign_up(self.client_class(), phone="0712345678")
        for identifier in ("0712 345 678", "+255712345678", "255712345678"):
            with self.subTest(identifier=identifier):
                client = self.client_class()
                client.post(LOGIN, {"username": identifier, "password": NEW_PASSWORD})
                self.assertTrue(get_user(client).is_authenticated)

    def test_no_new_paid_membership_without_a_confirmed_email(self):
        sign_up(self.client, phone="0712345678")
        member = User.objects.get(phone_number="+255712345678")
        with self.assertRaises(memberships.NotEligible) as refused:
            memberships.choose_plan(member, self.plan)
        self.assertEqual(refused.exception.code, "contact_unverified")
        detail = self.client.get(reverse("memberships:plan_detail", args=[self.plan.code]))
        self.assertContains(detail, "Add an email address to buy a membership")
        self.assertContains(detail, f'href="{EMAIL_CHANGE}"')
        self.assertNotContains(detail, "Choose this plan")
        response = self.client.post(reverse("memberships:choose", args=[self.plan.code]), follow=True)
        self.assertContains(response, "add an email address in My account")
        self.assertFalse(Subscription.objects.filter(member=member).exists())

    def test_adding_and_confirming_an_email_unlocks_payment_and_recovery(self):
        sign_up(self.client, phone="0712345678")
        member = User.objects.get(phone_number="+255712345678")
        self.client.post(EMAIL_CHANGE, {"new_email": "amina@example.com", "current_password": NEW_PASSWORD})
        link = path_of(links("amina@example.com", "/accounts/email-change/")[-1])
        self.client.post(self.client.get(link, follow=True).request["PATH_INFO"], {})
        member.refresh_from_db()
        self.assertEqual((member.email, member.email_verified), ("amina@example.com", True))
        self.assertTrue(has_verified_contact(member))
        self.assertEqual(memberships.choose_plan(member, self.plan).status, Subscription.Status.PENDING)
        mail.outbox.clear()
        self.client_class().post(RESET, {"email": "amina@example.com"})
        self.assertTrue(links("amina@example.com", "/accounts/reset/"))

    def test_the_number_cannot_be_removed_while_it_is_the_only_working_sign_in(self):
        sign_up(self.client, phone="0712345678")
        response = self.client.post(PROFILE_EDIT, {"first_name": "Amina", "last_name": "Juma", "phone_number": "",
                                                   "current_password": NEW_PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertIn("phone_number", response.context["form"].errors)
        self.assertEqual(User.objects.get(first_name="Amina").phone_number, "+255712345678")

    def test_phone_only_members_are_told_how_to_recover(self):
        page = self.client.get(RESET)
        self.assertContains(page, "Add a recovery email now in My account")
        self.assertContains(page, "Locked out with no email on your account?")


class ExistingAccountTests(ManualPaymentTestCase):
    def test_legacy_email_and_username_accounts_sign_in_as_before(self):
        legacy_email = make_user("legacy@example.com")
        legacy_username = User.objects.create_user(None, PASSWORD, username="old.member", status=User.Status.ACTIVE)
        for identifier, account in (("legacy@example.com", legacy_email), ("Old.Member", legacy_username)):
            with self.subTest(identifier=identifier):
                client = self.client_class()
                client.post(LOGIN, {"username": identifier, "password": PASSWORD})
                self.assertEqual(get_user(client).pk, account.pk)
        self.assertFalse(legacy_email.email_ownership_required)

    def test_a_payment_already_submitted_is_still_approved_without_a_verified_contact(self):
        claim = self.submit()  # Submitted while the member had a confirmed email.
        member = User.objects.get(pk=self.member.pk)
        member.phone_number, member.email, member.email_verified = "+255712345678", None, False
        member.save()
        self.assertFalse(has_verified_contact(member))
        approved, changed = self.approve(claim)
        self.assertTrue(changed)
        self.assertEqual(approved.status, ManualMembershipPayment.Status.APPROVED)
        self.assertEqual(Subscription.objects.get(pk=claim.subscription_id).status, Subscription.Status.ACTIVE)

    def test_legacy_pending_accounts_still_activate_by_choosing_a_password(self):
        from apps.accounts import services

        pending = User.objects.create_user("invited@example.com", None, status=User.Status.PENDING)
        self.assertFalse(services.is_awaiting_email_confirmation(pending))
        mail.outbox.clear()
        self.client_class().post(RESEND, {"email": "invited@example.com"})
        self.assertTrue(links("invited@example.com", "/accounts/activate/"))
        self.assertFalse(links("invited@example.com", "/accounts/confirm-email/"))


class PasswordSecurityEmailTests(ManualPaymentTestCase):
    def test_password_change_and_reset_notify_the_confirmed_address(self):
        member = make_user("owner@example.com")
        client = self.client_class()
        client.post(LOGIN, {"username": "owner@example.com", "password": PASSWORD})
        mail.outbox.clear()
        client.post(PASSWORD_CHANGE, {"old_password": PASSWORD, "new_password1": NEW_PASSWORD,
                                      "new_password2": NEW_PASSWORD})
        self.assertIn("Your ClipCash password was changed", subjects("owner@example.com"))
        mail.outbox.clear()
        self.client_class().post(RESET, {"email": "owner@example.com"})
        reset = path_of(links("owner@example.com", "/accounts/reset/")[-1])
        resetter = self.client_class()
        form_page = resetter.get(reset, follow=True)
        mail.outbox.clear()
        resetter.post(form_page.request["PATH_INFO"], {"new_password1": "Third-Strong-Phrase-5",
                                                      "new_password2": "Third-Strong-Phrase-5"})
        self.assertTrue(User.objects.get(pk=member.pk).check_password("Third-Strong-Phrase-5"))
        self.assertIn("Your ClipCash password was changed", subjects("owner@example.com"))
        self.assertEqual(resetter.get(reset, follow=True).status_code, 200)
        self.assertFalse(resetter.get(reset, follow=True).context.get("validlink", False))  # Single use.

    def test_no_security_email_to_an_unconfirmed_address(self):
        make_user("unconfirmed@example.com", verified=False)
        client = self.client_class()
        client.post(LOGIN, {"username": "unconfirmed@example.com", "password": PASSWORD})
        mail.outbox.clear()
        client.post(PASSWORD_CHANGE, {"old_password": PASSWORD, "new_password1": NEW_PASSWORD,
                                      "new_password2": NEW_PASSWORD})
        self.assertEqual(subjects("unconfirmed@example.com"), [])

    def test_notices_reach_unconfirmed_members_in_app_and_confirmed_ones_by_email_too(self):
        """PRD-03A notification queue: an email copy is queued only for a confirmed address."""
        from apps.notifications import inbox
        from apps.notifications.models import Notification

        sign_up(self.client_class(), email="amina@example.com")
        member = User.objects.get(email="amina@example.com")
        inbox.notify(member, "account.test", category=inbox.Category.ACCOUNT, title="Hello", dedupe_key="t:1",
                     email=True)
        self.assertTrue(Notification.objects.filter(recipient=member, kind="account.test").exists())
        self.assertFalse(OutboundEmail.objects.filter(recipient="amina@example.com").exists())
        User.objects.filter(pk=member.pk).update(email_verified=True)
        inbox.notify(User.objects.get(pk=member.pk), "account.test", category=inbox.Category.ACCOUNT,
                     title="Hello again", dedupe_key="t:2", email=True)
        self.assertEqual(OutboundEmail.objects.filter(recipient="amina@example.com").count(), 1)


PUBLIC_PRODUCTION =dict(DEBUG=False, RESTRICTED_DEMO_MODE=False, ALLOWED_HOSTS=["clipcash.co.tz"],
                         PUBLIC_BASE_URL="https://clipcash.co.tz")


class ProductionEmailCheckTests(ManualPaymentTestCase):
    def check_ids(self):
        return {message.id for message in run_checks(include_deployment_checks=True)}

    @override_settings(**PUBLIC_PRODUCTION, EMAIL_BACKEND="django.core.mail.backends.dummy.EmailBackend",
                       DEFAULT_FROM_EMAIL="ClipCash <no-reply@localhost>")
    def test_public_production_refuses_non_delivering_email_and_placeholder_sender(self):
        ids = self.check_ids()
        self.assertIn("notifications.E004", ids)
        self.assertIn("notifications.E005", ids)
        self.assertNotIn("notifications.W001", ids)

    @override_settings(**PUBLIC_PRODUCTION, EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
                       EMAIL_HOST="smtp.provider.example", EMAIL_USE_TLS=True, EMAIL_USE_SSL=False,
                       DEFAULT_FROM_EMAIL="ClipCash <no-reply@clipcash.co.tz>")
    def test_tls_smtp_with_a_real_sender_passes_the_email_checks(self):
        self.assertFalse({"notifications.E001", "notifications.E002", "notifications.E003", "notifications.E004",
                          "notifications.E005"} & self.check_ids())

    @override_settings(DEBUG=False, RESTRICTED_DEMO_MODE=True,
                       EMAIL_BACKEND="django.core.mail.backends.dummy.EmailBackend")
    def test_the_restricted_demo_keeps_a_warning(self):
        ids = self.check_ids()
        self.assertIn("notifications.W001", ids)
        self.assertNotIn("notifications.E004", ids)

    @override_settings(PUBLIC_BASE_URL="https://clipcash.co.tz", ALLOWED_HOSTS=["clipcash.co.tz", "testserver"])
    def test_emailed_links_use_the_configured_https_origin(self):
        member = self.client_class()
        sign_up(member, email="amina@example.com")
        member.post(RESEND)  # PRD-03A: confirmation is sent only on request.
        self.assertTrue(confirmation_link("amina@example.com").startswith("https://clipcash.co.tz/accounts/"))
        make_user("owner@example.com")
        self.client_class().post(RESET, {"email": "owner@example.com"})
        self.assertTrue(links("owner@example.com", "/accounts/reset/")[-1].startswith("https://clipcash.co.tz/"))
