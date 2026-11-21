"""
REG-03 acceptance, as corrected by PRD-01 (sign-up by email OR mobile number, no username), with
fictional test records only.

Covers usernames of EXISTING accounts (rules, normalization, uniqueness, signing in), the
required fields and contact choice of the sign-up form, normalized duplicates, existing members
and staff signing in as before, password reset (verified email only), the password rules
(members 8+ characters with a letter and a number, spaces allowed; staff unchanged), the Show
password control, and a mobile member reaching checkout once an email is confirmed. The favicon
(FAV-01) is covered at the end. PRD-01's email activation, phone restrictions and production
email checks are in test_prd01_email_first.
"""

import importlib.util
import struct
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user, password_validation
from django.contrib.staticfiles import finders
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from apps.accounts.access import identity_ready
from apps.accounts.forms import RegistrationForm
from apps.accounts.tests.helpers import NEW_PASSWORD, PASSWORD, make_user
from apps.accounts.usernames import validate_username
from apps.audit.models import AuditEvent
from apps.memberships.models import Subscription
from apps.notifications.models import OutboundEmail
from apps.payments.models import ManualMembershipPayment
from apps.payments.tests.manual_helpers import ManualPaymentTestCase, User

REGISTER = reverse("accounts:register")
LOGIN = reverse("accounts:login")
DASHBOARD = reverse("dashboard")
RESET = reverse("accounts:password_reset")
PROFILE = reverse("accounts:profile")
VERIFY = reverse("accounts:verification_required")
LETTER_AND_NUMBER = "Your password must contain at least one letter and at least one number."


def registration(contact_method, contact, password=NEW_PASSWORD, **extra):
    """The public sign-up form (PRD-03A): names, ONE of email, mobile number or username, password."""
    data = {"first_name": "Amina", "last_name": "Juma", "contact_method": contact_method, "email": "",
            "phone_number": "", "username": "", "password1": password, "password2": password}
    data[{"phone": "phone_number", "email": "email", "username": "username"}[contact_method]] = contact
    return {**data, **extra}


class FlexibleRegistrationTests(ManualPaymentTestCase):
    """ManualPaymentTestCase: registration open, a 5,000 TZS "monthly" plan, published Lipa
    details and manual payments switched on."""

    def register(self, contact_method, contact, client=None, **extra):
        return (client or self.client).post(REGISTER, registration(contact_method, contact, **extra))

    # --- The form ----------------------------------------------------------------------------

    def test_mobile_member_is_signed_in_and_ready_at_once(self):
        self.assertRedirects(self.register("phone", "0712 345 678"), DASHBOARD)
        user = User.objects.get(phone_number="+255712345678")
        self.assertEqual((user.first_name, user.last_name, user.email, user.username),
                         ("Amina", "Juma", None, None))
        self.assertFalse(user.phone_verified)  # No SMS: never verified automatically.
        self.assertTrue(identity_ready(user))
        self.assertEqual(get_user(self.client).pk, user.pk)
        self.assertEqual(self.client.get(DASHBOARD).status_code, 200)
        self.assertEqual(AuditEvent.objects.get(action="account.registered").metadata, {"method": "phone"})

    def test_only_the_chosen_contact_is_required_and_used(self):
        self.assertRedirects(self.register("phone", "0712345678", email="not-an-email"), DASHBOARD)
        user = User.objects.get(phone_number="+255712345678")
        self.assertEqual((user.email, user.phone_number), (None, "+255712345678"))  # The email box was ignored.
        form = RegistrationForm(registration("email", ""))
        self.assertFalse(form.is_valid())
        self.assertEqual(list(form.errors), ["email"])
        form = RegistrationForm(registration("phone", "", email="amina@example.com"))
        self.assertFalse(form.is_valid())
        self.assertEqual(list(form.errors), ["phone_number"])

    def test_names_and_password_are_required(self):
        form = RegistrationForm(registration("phone", "0712345678", first_name="", last_name=" ",
                                             password1="", password2=""))
        self.assertFalse(form.is_valid())
        self.assertEqual(set(form.errors), {"first_name", "last_name", "password1", "password2"})
        self.assertFalse(RegistrationForm({**registration("phone", "0712345678"), "contact_method": ""}).is_valid())

    def test_public_sign_up_accepts_a_username_again(self):
        # PRD-03A: username sign-up is back; only the chosen identifier is used.
        self.assertIn("username", RegistrationForm().fields)
        self.assertRedirects(self.register("phone", "0712345678", username="ignored"), DASHBOARD)
        self.assertIsNone(User.objects.get(phone_number="+255712345678").username)
        self.assertRedirects(self.register("username", "Zawadi.K", client=self.client_class()), DASHBOARD)
        self.assertIsNone(User.objects.get(username="zawadi.k").email)

    # --- Normalized duplicates and confusable usernames -----------------------------------

    def test_normalized_duplicates_are_refused(self):
        make_user("amina@example.com")
        User.objects.create_user(None, PASSWORD, phone_number="+255712345678", status=User.Status.ACTIVE)
        for value in ("0712-345-678", "255712345678"):
            with self.subTest(value=value):
                cache.clear()
                response = self.register("phone", value, client=self.client_class())
                self.assertEqual(response.status_code, 200)
                self.assertIn("phone_number", response.context["form"].errors)
        # PRD-03A: instant sign-up says the address is taken (rate-limited); nothing is created.
        cache.clear()
        response = self.register("email", "AMINA@Example.com", client=self.client_class())
        self.assertEqual(response.status_code, 200)
        self.assertIn("email", response.context["form"].errors)
        self.assertEqual(User.objects.filter(email="amina@example.com").count(), 1)
        self.assertEqual(User.objects.filter(phone_number="+255712345678").count(), 1)

    def test_usernames_cannot_be_confused_with_numbers_or_emails(self):
        for value in ("0712345678", "255712345678", "+255712345678", "7amina", "amina@example", "am", "a" * 31,
                      "amina juma", "amina..k", "amina.", "admin", "Support"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_username(value)
        for value in ("amina", "Amina_K", "a.k_2026"):
            with self.subTest(value=value):
                validate_username(value)

    def test_database_keeps_usernames_lowercase_and_unique(self):
        member = User.objects.create_user(None, PASSWORD, username="Zawadi", status=User.Status.ACTIVE)
        self.assertEqual(member.username, "zawadi")
        other = User.objects.create_user(None, PASSWORD, username="baraka", status=User.Status.ACTIVE)
        for value in ("ZAWADI", "zawadi"):  # Bypassing normalization: format check, then the unique index.
            with self.subTest(value=value), self.assertRaises(IntegrityError), transaction.atomic():
                User.objects.filter(pk=other.pk).update(username=value)

    # --- Signing in ----------------------------------------------------------------------

    def test_every_identifier_signs_in_in_any_format(self):
        member = User.objects.create_user("zawadi@example.com", PASSWORD, username="zawadi",
                                          phone_number="+255713000111", status=User.Status.ACTIVE)
        for identifier in ("zawadi", "ZaWaDi", "zawadi@example.com", "ZAWADI@EXAMPLE.COM", "0713 000 111",
                           "+255713000111", "255713000111"):
            with self.subTest(identifier=identifier):
                client = self.client_class()
                response = client.post(LOGIN, {"username": identifier, "password": PASSWORD})
                self.assertEqual(response.status_code, 302, identifier)
                self.assertEqual(get_user(client).pk, member.pk)

    def test_existing_members_and_staff_keep_signing_in_with_their_permissions(self):
        self.assertTrue(self.client.login(username="root@example.com", password=PASSWORD))
        root = get_user(self.client)
        self.assertEqual((root.role, root.is_staff, root.is_superuser), (User.Role.SUPER_ADMIN, True, True))
        client = self.client_class()
        self.assertTrue(client.login(username="VERIFIER@example.com", password=PASSWORD))
        staff = get_user(client)
        self.assertTrue(staff.has_perm("payments.verify_manual_payments"))
        phone_member = User.objects.create_user(None, PASSWORD, phone_number="+255754000222",
                                                status=User.Status.ACTIVE)
        self.assertTrue(self.client_class().login(username="0754 000 222", password=PASSWORD))
        self.assertIsNone(phone_member.username)

    def test_wrong_password_for_a_username_is_rate_limited(self):
        User.objects.create_user(None, PASSWORD, username="zawadi", status=User.Status.ACTIVE)
        for _attempt in range(5):
            self.client.post(LOGIN, {"username": "zawadi", "password": "Wrong-Password-1"})
        response = self.client.post(LOGIN, {"username": "ZAWADI", "password": PASSWORD})  # Same account, any case.
        self.assertEqual([e.code for e in response.context["form"].non_field_errors().as_data()], ["throttled"])
        self.assertIsNone(get_user(self.client).pk)

    # --- Password reset ------------------------------------------------------------------

    def test_only_an_eligible_email_receives_a_reset_link(self):
        """PRD-03A: a confirmed address, or an email sign-up's own (only) address. Never a username,
        a number, or an unconfirmed address on an account that signs in with something else."""
        make_user("verified@example.com", verified=True)
        User.objects.create_user("second@example.com", PASSWORD, username="zawadi", status=User.Status.ACTIVE)
        User.objects.create_user(None, PASSWORD, phone_number="+255712345678", status=User.Status.ACTIVE)
        for typed in ("second@example.com", "zawadi", "0712345678"):
            with self.subTest(typed=typed):
                mail.outbox.clear()
                self.client.post(RESET, {"email": typed})
                self.assertEqual(mail.outbox, [])
        self.client.post(RESET, {"email": "Verified@Example.com"})
        self.assertEqual(mail.outbox[-1].to, ["verified@example.com"])

    def test_account_settings_explain_password_reset(self):
        self.register("phone", "0712345678")
        page = self.client.get(PROFILE)
        self.assertContains(page, "+255712345678")
        self.assertContains(page, "A password-reset link can only go to an email address you added and confirmed.")
        self.assertContains(page, "A username or a mobile number is never enough to reset a password.")

    # --- Passwords ------------------------------------------------------------------------

    def refused_codes(self, password, user):
        try:
            password_validation.validate_password(password, user)
        except ValidationError as error:
            return [item.code for item in error.error_list]
        return []

    def test_member_password_rules(self):
        member = User(first_name="Amina", last_name="Juma", username="amina.juma", role=User.Role.MEMBER)
        refused = {
            "plum7ox": "password_too_short",  # 7 characters.
            "plum ox": "password_too_short",
            "orangekites": "password_needs_letter_and_number",  # No number.
            "orange kites fly": "password_needs_letter_and_number",
            "1234 5678 90": "password_needs_letter_and_number",  # No letter (spaces are not letters).
            "1234567890123": "password_entirely_numeric",
            "password123": "password_too_common",
            "amina.juma1": "password_too_similar",
        }
        for password, code in refused.items():
            with self.subTest(password=password):
                self.assertIn(code, self.refused_codes(password, member))
        for password in ("plum7 ox", "zebra8kq", "mango river 7 lamps", "Another-Strong-Phrase-42"):
            with self.subTest(password=password):  # 8 characters is enough; spaces are fine.
                self.assertEqual(self.refused_codes(password, member), [])

    def test_staff_password_policy_is_unchanged(self):
        for role in (User.Role.SUPER_ADMIN, User.Role.CONTENT_MANAGER):
            staff = User(email="staff@example.com", role=role)
            with self.subTest(role=role):
                self.assertIn("password_too_short", self.refused_codes("zebra8kq", staff))  # 8 < 10.
                self.assertIn("password_too_short", self.refused_codes("zebra8kq0", staff))  # 9 < 10.
                self.assertEqual(self.refused_codes("orangekitesfly", staff), [])  # No number rule for staff.
                self.assertIn("password_too_common", self.refused_codes("password123", staff))
        self.assertIn("password_too_short", self.refused_codes("zebra8kq", None))  # Unknown account: staff policy.

    def test_member_policy_on_registration_change_and_reset(self):
        form = RegistrationForm(registration("phone", "0712345678", password="plum7ox"))
        self.assertIn("password1", form.errors)  # 7 characters.
        self.assertRedirects(self.register("phone", "0712345678", password="plum7 ox"), DASHBOARD)
        change = reverse("accounts:password_change")
        response = self.client.post(change, {"old_password": "plum7 ox", "new_password1": "orangekites",
                                             "new_password2": "orangekites"})
        self.assertIn(LETTER_AND_NUMBER, str(response.context["form"].errors))  # No number.
        self.assertRedirects(self.client.post(change, {"old_password": "plum7 ox", "new_password1": "kiwi 4 me",
                                                       "new_password2": "kiwi 4 me"}),
                             reverse("accounts:password_change_done"))
        member = make_user("reset@example.com", verified=True)
        from django.utils.encoding import force_bytes

        from apps.accounts.tokens import password_reset_token_generator as default_token_generator
        from django.utils.http import urlsafe_base64_encode

        uid = urlsafe_base64_encode(force_bytes(member.pk))
        page = self.client_class()
        first = page.get(reverse("accounts:password_reset_confirm", args=[uid, default_token_generator.make_token(member)]))
        self.assertContains(page.get(first.url), "at least 8 characters")
        too_short = page.post(first.url, {"new_password1": "pear5ok", "new_password2": "pear5ok"})
        self.assertIn("at least 8 characters", str(too_short.context["form"].errors))  # 7 characters.
        self.assertRedirects(page.post(first.url, {"new_password1": "pear 5 ok", "new_password2": "pear 5 ok"}),
                             reverse("accounts:password_reset_complete"))
        member.refresh_from_db()
        self.assertTrue(member.check_password("pear 5 ok"))

    def test_staff_password_change_keeps_the_staff_policy(self):
        self.client.force_login(self.root)
        change = reverse("accounts:password_change")
        page = self.client.get(change)
        self.assertContains(page, "Staff accounts need at least 10 characters")
        response = self.client.post(change, {"old_password": PASSWORD, "new_password1": "zebra8kq",
                                             "new_password2": "zebra8kq"})
        self.assertIn("at least 10 characters", str(response.context["form"].errors))  # Django reports it on
        # new_password2.

    def test_existing_passwords_keep_signing_in(self):
        member = User.objects.create_user(None, PASSWORD, username="legacy", status=User.Status.ACTIVE)
        staff = make_user("legacy-staff@example.com", role=User.Role.CONTENT_MANAGER)
        for account in (member, staff):  # Stored before these rules (no number, too short for staff).
            account.set_password("legacypass")
            account.save(update_fields=["password"])
        self.assertTrue(self.client_class().login(username="legacy", password="legacypass"))
        self.assertTrue(self.client_class().login(username="legacy-staff@example.com", password="legacypass"))

    def test_registration_shows_guidance_and_show_password_buttons(self):
        page = self.client.get(REGISTER)
        self.assertContains(page, "Spaces are allowed")
        self.assertContains(page, "Use at least 8 characters, with at least one letter and at least one number.")
        self.assertContains(page, "Your password must contain at least 8 characters.")
        self.assertNotContains(page, "at least 10 characters")
        for field in ("id_password1", "id_password2"):
            self.assertContains(page, f'aria-controls="{field}"')
            self.assertContains(page, f'data-password-target="{field}"')
        self.assertContains(self.client.get(LOGIN), 'data-password-target="id_password"')
        weak = self.register("phone", "0712345678", password="no numbers here at all")
        self.assertIn("password1", weak.context["form"].errors)
        self.assertFalse(User.objects.filter(phone_number="+255712345678").exists())

    # --- Checkout ------------------------------------------------------------------------

    def test_mobile_member_checkout_offers_their_number(self):
        self.register("phone", "0712 345 678")
        # PRD-01: a confirmed email address first (the email-change flow; see test_prd01_email_first).
        User.objects.filter(phone_number="+255712345678").update(email="amina@example.com", email_verified=True)
        self.client.force_login(User.objects.get(phone_number="+255712345678"))  # Session follows the identifier.
        self.client.post(reverse("memberships:choose", args=["monthly"]))
        pending = Subscription.objects.get(member__phone_number="+255712345678", status=Subscription.Status.PENDING)
        checkout = self.client.get(reverse("payments:manual_pay", args=[pending.pk]))
        self.assertEqual(checkout.context["form"]["payer_phone"].value(), "+255712345678")

    def test_no_outbound_email_for_mobile_sign_up(self):
        self.register("phone", "0712345678")
        self.assertFalse(OutboundEmail.objects.exists())


class FaviconTests(TestCase):
    """FAV-01: one set of icon links on every page (site and admin), raster files for clients
    that ignore SVG, and /favicon.ico answered."""

    LINKS = ('rel="icon" href="/static/images/favicon.ico" sizes="32x32"',
             'rel="icon" href="/static/images/favicon.svg" type="image/svg+xml"',
             'rel="apple-touch-icon" href="/static/images/apple-touch-icon.png"')

    def test_site_and_admin_pages_link_every_icon(self):
        for url in (reverse("home"), LOGIN, REGISTER, reverse("admin:login")):
            page = self.client.get(url).content.decode()
            for link in self.LINKS:
                with self.subTest(url=url, link=link):
                    self.assertIn(link, page)

    def test_favicon_ico_path_redirects_to_the_static_icon(self):
        response = self.client.get("/favicon.ico")
        self.assertRedirects(response, "/static/images/favicon.ico", fetch_redirect_response=False)
        self.assertEqual(response["Cache-Control"], "public, max-age=86400")
        self.assertEqual(self.client.post("/favicon.ico").status_code, 405)


class FaviconAssetTests(SimpleTestCase):
    def test_icon_files_are_found_by_collectstatic(self):
        for name in ("images/favicon.svg", "images/favicon.ico", "images/apple-touch-icon.png"):
            with self.subTest(name=name):
                self.assertIsNotNone(finders.find(name))

    def test_ico_holds_16_32_and_48_pixel_images(self):
        data = Path(finders.find("images/favicon.ico")).read_bytes()
        reserved, kind, count = struct.unpack("<HHH", data[:6])
        self.assertEqual((reserved, kind), (0, 1))
        sizes = [data[6 + 16 * i] for i in range(count)]
        self.assertEqual(sizes, [16, 32, 48])

    def test_raster_icons_match_the_generator(self):
        """The committed files are exactly what scripts/generate_favicons.py draws from the SVG
        geometry, so the icons cannot drift apart."""
        spec = importlib.util.spec_from_file_location(
            "generate_favicons", Path(settings.BASE_DIR) / "scripts" / "generate_favicons.py")
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        expected_ico = generator.ico([(size, generator.png(generator.render(size))) for size in (16, 32, 48)])
        expected_touch = generator.png(generator.render(180, rounded=False), opaque=True)
        self.assertEqual(Path(finders.find("images/favicon.ico")).read_bytes(), expected_ico)
        self.assertEqual(Path(finders.find("images/apple-touch-icon.png")).read_bytes(), expected_touch)
