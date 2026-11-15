"""
PRD-01 acceptance: email-first authentication, with fictional test records only.

Members sign up with an email address OR a Tanzanian mobile number (no username). An email
sign-up stays PENDING until its emailed link is opened, and cannot sign in or reset a password
before that. A mobile sign-up is ACTIVE at once with an unverified number (no SMS in this
phase): it must add and confirm an email address before starting a paid membership or
recovering a password. Password recovery is by confirmed email only. Existing accounts,
memberships and submitted payments keep working.
"""

import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user
from django.core import mail
from django.core.checks import run_checks
from django.test import override_settings
from django.urls import reverse

from apps.accounts import tokens
from apps.accounts.access import has_verified_contact
from apps.accounts.tests.helpers import NEW_PASSWORD, PASSWORD, make_user
from apps.audit.models import AuditEvent
from apps.memberships import services as memberships
from apps.memberships.models import Subscription
from apps.payments.models import ManualMembershipPayment
from apps.payments.tests.manual_helpers import ManualPaymentTestCase, User

REGISTER = reverse("accounts:register")
REGISTER_DONE = reverse("accounts:register_done")
LOGIN = reverse("accounts:login")
DASHBOARD = reverse("dashboard")
RESET = reverse("accounts:password_reset")
RESEND = reverse("accounts:resend_verification")
EMAIL_CHANGE = reverse("accounts:email_change")
PROFILE_EDIT = reverse("accounts:profile_edit")
PASSWORD_CHANGE = reverse("accounts:password_change")


def sign_up(client, *, email=None, phone=None, password=NEW_PASSWORD):
    data = {"first_name": "Amina", "last_name": "Juma", "password1": password, "password2": password,
            "contact_method": "email" if email else "phone", "email": email or "", "phone_number": phone or ""}
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


class EmailSignUpTests(ManualPaymentTestCase):
    """ManualPaymentTestCase: registration open, a 5,000 TZS plan, published Lipa details,
    manual payments on, and a client that delivers queued email after each request."""

    def test_the_form_asks_for_email_or_phone_and_no_username(self):
        page = self.client.get(REGISTER)
        form = page.context["form"]
        self.assertEqual(list(form.fields),
                         ["first_name", "last_name", "contact_method", "email", "phone_number", "password1",
                          "password2"])
        self.assertContains(page, "you must add an email address in My account and confirm it")

    def test_email_sign_up_is_pending_until_the_link_is_opened(self):
        response = sign_up(self.client, email="Amina@Example.com")
        self.assertRedirects(response, REGISTER_DONE)
        member = User.objects.get(email="amina@example.com")
        self.assertEqual((member.status, member.email_verified, member.email_ownership_required, member.is_active),
                         (User.Status.PENDING, False, True, False))
        self.assertFalse(get_user(self.client).is_authenticated)  # Not signed in by sign-up.
        self.assertIsNone(member.username)
        self.assertEqual(AuditEvent.objects.get(action="account.registered").metadata, {"method": "email"})
        self.assertEqual(subjects("amina@example.com"), ["Confirm your ClipCash email address"])
        self.assertIn("activate your account", mail.outbox[-1].body)

        # Before the link: no sign-in (generic message) and no reset link.
        refused = self.client_class().post(LOGIN, {"username": "amina@example.com", "password": NEW_PASSWORD})
        self.assertEqual([e.code for e in refused.context["form"].non_field_errors().as_data()], ["invalid_login"])
        mail.outbox.clear()
        self.client_class().post(RESET, {"email": "amina@example.com"})
        self.assertEqual(mail.outbox, [])

    def test_opening_the_link_activates_and_the_member_signs_in(self):
        sign_up(self.client_class(), email="amina@example.com")
        link = path_of(confirmation_link("amina@example.com"))
        visitor = self.client_class()
        self.assertContains(visitor.get(link), "Confirm")  # GET only shows a button (link scanners).
        self.assertEqual(User.objects.get(email="amina@example.com").status, User.Status.PENDING)
        done = visitor.post(link)
        self.assertContains(done, "Account activated")
        member = User.objects.get(email="amina@example.com")
        self.assertEqual((member.status, member.email_verified, member.is_active), (User.Status.ACTIVE, True, True))
        self.assertTrue(has_verified_contact(member))
        self.assertFalse(get_user(visitor).is_authenticated)  # A link never signs anyone in.
        self.assertTrue(AuditEvent.objects.filter(action="account.activated", target_id=str(member.pk)).exists())

        signed_in = self.client_class()
        signed_in.post(LOGIN, {"username": "AMINA@example.com", "password": NEW_PASSWORD})
        self.assertEqual(get_user(signed_in).pk, member.pk)
        self.assertEqual(signed_in.get(DASHBOARD).status_code, 200)
        self.assertEqual(memberships.choose_plan(member, self.plan).status, Subscription.Status.PENDING)

    def test_the_link_works_once(self):
        sign_up(self.client_class(), email="amina@example.com")
        link = path_of(confirmation_link("amina@example.com"))
        self.assertEqual(self.client_class().post(link).status_code, 200)
        self.assertEqual(self.client_class().post(link).status_code, 400)
        self.assertEqual(self.client_class().get(link).status_code, 400)

    def test_an_expired_link_is_refused_and_a_new_one_can_be_requested(self):
        sign_up(self.client_class(), email="amina@example.com")
        link = path_of(confirmation_link("amina@example.com"))
        later = tokens.verification_token_generator._now() + timedelta(hours=25)
        with mock.patch.object(tokens.AccountVerificationTokenGenerator, "_now", lambda self: later):
            self.assertEqual(self.client_class().post(link).status_code, 400)
        self.assertEqual(User.objects.get(email="amina@example.com").status, User.Status.PENDING)
        mail.outbox.clear()
        self.client_class().post(RESEND, {"email": "amina@example.com"})
        fresh = confirmation_link("amina@example.com")
        self.assertIsNotNone(fresh)
        self.assertEqual(self.client_class().post(path_of(fresh)).status_code, 200)
        self.assertEqual(User.objects.get(email="amina@example.com").status, User.Status.ACTIVE)

    def test_the_old_activation_page_cannot_be_used_for_an_email_sign_up(self):
        sign_up(self.client_class(), email="amina@example.com")
        link = path_of(confirmation_link("amina@example.com")).replace("/confirm-email/", "/activate/")
        self.assertEqual(self.client_class().get(link, follow=True).status_code, 200)
        member = User.objects.get(email="amina@example.com")
        self.assertEqual(member.status, User.Status.PENDING)
        self.assertTrue(member.check_password(NEW_PASSWORD))  # The password could not be replaced.

    def test_a_registered_address_is_not_revealed(self):
        existing = make_user("taken@example.com")
        mail.outbox.clear()
        response = sign_up(self.client_class(), email="Taken@example.com")
        self.assertRedirects(response, REGISTER_DONE, fetch_redirect_response=False)
        self.assertEqual(User.objects.filter(email="taken@example.com").count(), 1)
        self.assertEqual(subjects("taken@example.com"), [mail.outbox[-1].subject])
        self.assertIn("already has an account", mail.outbox[-1].body)
        self.assertTrue(User.objects.get(pk=existing.pk).check_password(PASSWORD))  # Unchanged.
        self.assertTrue(AuditEvent.objects.filter(action="account.registration.duplicate").exists())

    def test_signing_up_again_before_confirming_sends_a_fresh_link_to_the_same_account(self):
        sign_up(self.client_class(), email="amina@example.com")
        mail.outbox.clear()
        self.assertRedirects(sign_up(self.client_class(), email="amina@example.com", password="Other-Phrase-77"),
                             REGISTER_DONE, fetch_redirect_response=False)
        self.assertEqual(User.objects.filter(email="amina@example.com").count(), 1)
        self.assertIsNotNone(confirmation_link("amina@example.com"))
        self.assertTrue(User.objects.get(email="amina@example.com").check_password(NEW_PASSWORD))

    def test_email_sign_up_is_rate_limited_per_address(self):
        for _attempt in range(10):
            sign_up(self.client_class(), email="busy@example.com")
        refused = sign_up(self.client_class(), email="busy@example.com")
        self.assertEqual(refused.status_code, 200)
        self.assertContains(refused, "Too many requests")


class PhoneSignUpTests(ManualPaymentTestCase):
    def test_phone_sign_up_is_active_and_signed_in_but_unverified(self):
        self.assertRedirects(sign_up(self.client, phone="0712 345 678"), DASHBOARD, fetch_redirect_response=False)
        member = User.objects.get(phone_number="+255712345678")
        self.assertEqual((member.status, member.phone_verified, member.email, member.username),
                         (User.Status.ACTIVE, False, None, None))
        self.assertEqual(get_user(self.client).pk, member.pk)
        self.assertFalse(has_verified_contact(member))
        self.assertEqual(mail.outbox, [])
        page = self.client.get(DASHBOARD)
        self.assertContains(page, "Add a recovery email address")
        self.assertContains(page, f'href="{EMAIL_CHANGE}"')

    def test_number_signs_in_in_any_format(self):
        sign_up(self.client_class(), phone="0712345678")
        for identifier in ("0712 345 678", "+255712345678", "255712345678"):
            with self.subTest(identifier=identifier):
                client = self.client_class()
                client.post(LOGIN, {"username": identifier, "password": NEW_PASSWORD})
                self.assertTrue(get_user(client).is_authenticated)

    def test_a_taken_number_is_refused(self):
        sign_up(self.client_class(), phone="0712345678")
        refused = sign_up(self.client_class(), phone="+255 712 345 678")
        self.assertEqual(refused.status_code, 200)
        self.assertIn("phone_number", refused.context["form"].errors)
        self.assertEqual(User.objects.filter(phone_number="+255712345678").count(), 1)

    def test_no_new_paid_membership_without_a_confirmed_email(self):
        sign_up(self.client, phone="0712345678")
        member = User.objects.get(phone_number="+255712345678")
        with self.assertRaises(memberships.NotEligible) as refused:
            memberships.choose_plan(member, self.plan)
        self.assertEqual(refused.exception.code, "contact_unverified")
        detail = self.client.get(reverse("memberships:plan_detail", args=[self.plan.code]))
        self.assertContains(detail, "Add a recovery email address")
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
        self.assertContains(page, "Locked out with no confirmed email?")
        mail.outbox.clear()
        sign_up(self.client_class(), phone="0712345678")
        self.client_class().post(RESET, {"email": "0712345678@example.com"})
        self.assertEqual(mail.outbox, [])


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


PUBLIC_PRODUCTION = dict(DEBUG=False, RESTRICTED_DEMO_MODE=False, ALLOWED_HOSTS=["clipcash.co.tz"],
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
        sign_up(self.client_class(), email="amina@example.com")
        self.assertTrue(confirmation_link("amina@example.com").startswith("https://clipcash.co.tz/accounts/"))
        make_user("owner@example.com")
        self.client_class().post(RESET, {"email": "owner@example.com"})
        self.assertTrue(links("owner@example.com", "/accounts/reset/")[-1].startswith("https://clipcash.co.tz/"))
