"""Phase 17-A: profile editing, the email-change workflow and security information."""

from datetime import timedelta
from unittest import mock
from urllib.parse import urlsplit

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user
from django.core import mail
from django.db import IntegrityError, transaction
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts import reauth, services, views
from apps.accounts.models import PendingEmailChange
from apps.accounts.throttling import EMAIL_REQUESTS_PER_IP
from apps.accounts.tokens import email_change_token_generator, verification_token_generator
from apps.audit.models import AuditEvent
from apps.core import features
from apps.notifications.models import OutboundEmail

from .helpers import NEW_PASSWORD, PASSWORD, AccountsTestCase, User, make_user

PROFILE = reverse("accounts:profile")
EDIT = reverse("accounts:profile_edit")
EMAIL_CHANGE = reverse("accounts:email_change")
CANCEL = reverse("accounts:email_change_cancel")
PASSWORD_CHANGE = reverse("accounts:password_change")
LOGIN = reverse("accounts:login")
VERIFY = reverse("accounts:verification_required")
NEW_EMAIL = "new-address@example.com"
DEMO = override_settings(RESTRICTED_DEMO_MODE=True, DEMO_EMAIL_VERIFICATION_OPTIONAL=True,
                         DEMO_ACCOUNTS=["demo@example.com"], PAYMENTS_MODE="disabled",
                         PAYMENTS_SANDBOX_ALLOWED=False)


def events(action):
    return AuditEvent.objects.filter(action=action)


def link_sent_to(address):
    """Path of the link in the newest email to ``address``, or None."""
    for message in reversed(mail.outbox):
        if address in message.to:
            for word in message.body.split():
                if word.startswith("http"):
                    return urlsplit(word).path
    return None


class SelfServiceTestCase(AccountsTestCase):
    def setUp(self):
        super().setUp()
        self.member = make_user()
        self.client.force_login(self.member)

    def request_change(self, new_email=NEW_EMAIL, password=PASSWORD):
        return self.client.post(EMAIL_CHANGE, {"new_email": new_email, "current_password": password})

    def confirm(self, link, password=PASSWORD, client=None):
        """Open the emailed link, then complete the change on the review page (RG-03).
        Returns the first response that is not the link's redirect."""
        client = client or self.client
        opened = client.get(link)
        if opened.status_code != 302:
            return opened
        return client.post(opened.url, {"current_password": password})


class ProfileEditTests(SelfServiceTestCase):
    def save(self, **data):
        return self.client.post(EDIT, {"first_name": "Asha", "last_name": "Juma", "phone_number": "", **data})

    def test_owner_edits_name_and_phone_in_any_accepted_format(self):
        self.assertRedirects(self.save(phone_number="0712 345 678"), PROFILE)
        self.member.refresh_from_db()
        self.assertEqual((self.member.first_name, self.member.last_name, self.member.phone_number),
                         ("Asha", "Juma", "+255712345678"))

    def test_phone_can_be_removed(self):
        self.save(phone_number="+255712345678")
        self.save(phone_number="")
        self.member.refresh_from_db()
        self.assertIsNone(self.member.phone_number)

    def test_requires_a_signed_in_owner_with_verified_email(self):
        self.client.logout()
        self.assertEqual(self.client.get(EDIT).status_code, 302)
        self.client.force_login(make_user("other@example.com", verified=False))
        self.assertRedirects(self.client.get(EDIT), VERIFY)

    def test_cannot_escalate_or_touch_protected_fields(self):
        self.save(role=User.Role.SUPER_ADMIN, status=User.Status.DISABLED, is_staff="on", is_superuser="on",
                  email="stolen@example.com", email_verified="", password="x", id=str(make_user("v@example.com").pk))
        self.member.refresh_from_db()
        self.assertEqual(self.member.role, User.Role.MEMBER)
        self.assertEqual(self.member.status, User.Status.ACTIVE)
        self.assertFalse(self.member.is_staff or self.member.is_superuser)
        self.assertEqual(self.member.email, "member@example.com")
        self.assertTrue(self.member.email_verified and self.member.check_password(PASSWORD))

    def test_only_the_signed_in_account_changes(self):
        other = make_user("other@example.com")
        self.save(id=str(other.pk), pk=str(other.pk), user=str(other.pk))
        other.refresh_from_db()
        self.assertEqual(other.first_name, "")

    def test_duplicate_phone_is_refused_without_naming_the_owner(self):
        User.objects.filter(pk=make_user("holder@example.com").pk).update(phone_number="+255712345678")
        response = self.save(phone_number="0712345678")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This phone number cannot be used")
        self.assertNotContains(response, "holder@example.com")
        self.assertNotContains(response, "already exists")
        self.member.refresh_from_db()
        self.assertIsNone(self.member.phone_number)

    def test_invalid_phone_is_refused(self):
        self.assertEqual(self.save(phone_number="not-a-phone").status_code, 200)
        self.member.refresh_from_db()
        self.assertIsNone(self.member.phone_number)

    def test_phone_attempts_are_limited_and_stop_revealing_duplicates(self):
        User.objects.filter(pk=make_user("holder@example.com").pk).update(phone_number="+255712345678")
        for number in range(10):
            self.save(phone_number=f"+25571234{number:04d}")
        response = self.save(phone_number="+255712345678")
        self.assertContains(response, "Too many phone number changes")
        self.assertNotContains(response, "This phone number cannot be used")

    def test_audit_names_the_fields_but_never_their_values(self):
        self.save(phone_number="+255712345678")
        event = events("account.profile_updated").get()
        self.assertEqual(event.metadata, {"changed_fields": ["first_name", "last_name", "phone_number"]})
        self.assertEqual(event.actor_id, self.member.pk)
        self.assertNotIn("255712345678", str(event.metadata))

    def test_saving_without_changes_records_nothing(self):
        self.save()
        self.save()
        self.assertEqual(events("account.profile_updated").count(), 1)

    def test_stale_account_data_cannot_undo_an_admin_decision(self):
        stale = User.objects.get(pk=self.member.pk)
        User.objects.filter(pk=self.member.pk).update(status=User.Status.SUSPENDED, is_active=False)
        services.update_profile(stale, first_name="A", last_name="", phone_number=None)
        self.member.refresh_from_db()
        self.assertEqual(self.member.status, User.Status.SUSPENDED)
        self.assertEqual(self.member.first_name, "A")

    def test_name_is_escaped_where_it_is_shown(self):
        self.save(first_name="<script>alert(1)</script>")
        response = self.client.get(PROFILE)
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;")

    def test_csrf_is_enforced(self):
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(self.member)
        for url, data in ((EDIT, {"first_name": "X"}), (CANCEL, {}),
                          (EMAIL_CHANGE, {"new_email": NEW_EMAIL, "current_password": PASSWORD}),
                          (PASSWORD_CHANGE, {"old_password": PASSWORD})):
            with self.subTest(url=url):
                self.assertEqual(client.post(url, data).status_code, 403)

    def test_staff_keep_their_role(self):
        for user in (make_user("cm@example.com", role=User.Role.CONTENT_MANAGER),
                     make_user("root@example.com", role=User.Role.SUPER_ADMIN)):
            role = user.role
            with self.subTest(role=role):
                self.client.force_login(user)
                self.assertEqual(self.client.get(EDIT).status_code, 200)
                self.assertRedirects(self.save(role=User.Role.MEMBER), PROFILE)
                user.refresh_from_db()
                self.assertEqual((user.role, user.first_name), (role, "Asha"))


class PasswordChangeTests(SelfServiceTestCase):
    def change(self, old=PASSWORD):
        return self.client.post(PASSWORD_CHANGE, {"old_password": old, "new_password1": NEW_PASSWORD,
                                                  "new_password2": NEW_PASSWORD})

    def test_wrong_current_password_is_refused_and_audited(self):
        self.assertEqual(self.change("wrong-password-123").status_code, 200)
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password(PASSWORD))
        event = events("auth.current_password.failed").get()
        self.assertEqual(event.metadata, {"operation": "password_change", "throttled": False})
        self.assertEqual(event.target_id, str(self.member.pk))
        self.assertNotIn("wrong-password-123", str(event.metadata))

    def test_guessing_is_limited_even_for_a_correct_guess(self):
        for _ in range(5):
            self.change("wrong-password-123")
        response = self.change()
        self.assertEqual(response.context["form"].errors.as_data()["old_password"][0].code, "throttled")
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password(PASSWORD))
        self.assertTrue(events("auth.current_password.failed").filter(outcome="DENIED").exists())

    def test_limit_is_shared_with_the_email_change_form(self):
        for _ in range(5):
            self.request_change(password="wrong-password-123")
        self.assertEqual(self.change().status_code, 200)
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password(PASSWORD))

    def test_correct_password_clears_earlier_failures(self):
        for _ in range(4):
            self.change("wrong-password-123")
        self.request_change()
        for _ in range(4):
            self.change("wrong-password-123")
        self.assertRedirects(self.change(), reverse("accounts:password_change_done"))

    def test_session_stays_and_change_works(self):
        self.assertRedirects(self.change(), reverse("accounts:password_change_done"))
        self.assertTrue(get_user(self.client).is_authenticated)
        self.assertTrue(User.objects.get(pk=self.member.pk).check_password(NEW_PASSWORD))


class EmailChangeRequestTests(SelfServiceTestCase):
    def test_nothing_changes_until_confirmed(self):
        self.assertRedirects(self.request_change(), PROFILE)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, "member@example.com")
        self.assertTrue(self.member.email_verified)
        self.assertEqual(PendingEmailChange.objects.get().new_email, NEW_EMAIL)
        self.assertContains(self.client.get(PROFILE), NEW_EMAIL)

    def test_address_is_normalized(self):
        self.request_change(" New-Address@Example.COM ")
        self.assertEqual(PendingEmailChange.objects.get().new_email, NEW_EMAIL)

    def test_current_password_is_required(self):
        for password in ("", "wrong-password-123"):
            with self.subTest(password=password):
                self.assertEqual(self.request_change(password=password).status_code, 200)
        self.assertFalse(PendingEmailChange.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_own_address_is_refused(self):
        response = self.request_change("MEMBER@example.com")
        self.assertContains(response, "already your email address")
        self.assertFalse(PendingEmailChange.objects.exists())

    def test_anonymous_and_unverified_are_turned_away(self):
        self.client.logout()
        self.assertEqual(self.request_change().status_code, 302)
        self.client.force_login(make_user("other@example.com", verified=False))
        self.assertRedirects(self.request_change(), VERIFY)
        self.assertFalse(PendingEmailChange.objects.exists())

    def test_link_goes_to_new_address_and_a_warning_to_the_current_one(self):
        self.request_change()
        self.assertIn("email-change", link_sent_to(NEW_EMAIL))
        warning = next(message for message in mail.outbox if "member@example.com" in message.to)
        self.assertNotIn(NEW_EMAIL, warning.body)
        self.assertNotIn("email-change", warning.body)

    def test_registered_address_looks_the_same_but_gets_no_link(self):
        make_user("taken@example.com")
        free = self.request_change()
        taken_client = self.client_class()
        taken_client.force_login(make_user("second@example.com"))
        taken = taken_client.post(EMAIL_CHANGE, {"new_email": "taken@example.com", "current_password": PASSWORD})
        self.assertEqual((free.status_code, free["Location"]), (taken.status_code, taken["Location"]))
        self.assertContains(taken_client.get(PROFILE), "taken@example.com")
        self.assertIsNone(link_sent_to("taken@example.com"))
        self.assertFalse(events("account.email_change.requested").filter(
            metadata__address_available=True, actor_id=User.objects.get(email="second@example.com").pk).exists())

    def test_requests_are_limited_per_account(self):
        for number in range(5):
            self.assertRedirects(self.request_change(f"new{number}@example.com"), PROFILE)
        response = self.request_change("one-more@example.com")
        self.assertContains(response, "Too many requests")
        self.assertEqual(PendingEmailChange.objects.get().new_email, "new4@example.com")
        self.assertEqual(events("account.email_change.denied").get().metadata, {"reason": "throttled"})

    def test_requests_are_limited_per_address_without_affecting_its_owners_reset(self):
        for name in ("a", "b", "c"):
            self.client.force_login(make_user(f"{name}@example.com"))
            self.request_change("victim@example.com")
        self.client.force_login(make_user("d@example.com"))
        self.assertContains(self.request_change("victim@example.com"), "Too many requests")

        victim = make_user("victim@example.com")
        self.client.logout()
        self.client.post(reverse("accounts:password_reset"), {"email": victim.email})
        self.assertIn("reset", link_sent_to(victim.email))

    def test_requests_are_limited_per_ip(self):
        address = "203.0.113.9"
        for _ in range(EMAIL_REQUESTS_PER_IP.limit):
            EMAIL_REQUESTS_PER_IP.hit(address)
        with mock.patch("apps.accounts.views.client_ip_for_limits", return_value=address):
            self.assertContains(self.request_change(), "Too many requests")
        self.assertFalse(PendingEmailChange.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_audit_and_outbox_hold_no_token_or_plain_new_address_in_metadata(self):
        self.request_change()
        event = events("account.email_change.requested").get()
        self.assertEqual(set(event.metadata), {"new_address_hash", "address_available"})
        self.assertNotIn(NEW_EMAIL, str(event.metadata))
        path = link_sent_to(NEW_EMAIL)
        token = path.rstrip("/").rsplit("/", 1)[-1]
        for email in OutboundEmail.objects.all():
            self.assertNotIn(token, str(email.context))
        self.assertFalse(AuditEvent.objects.filter(metadata__icontains=token).exists())


class EmailChangeConfirmTests(SelfServiceTestCase):
    def link(self, new_email=NEW_EMAIL):
        self.request_change(new_email)
        return link_sent_to(new_email)

    def test_opening_the_link_changes_nothing(self):
        link = self.link()
        response = self.client.get(link, follow=True)
        self.assertContains(response, NEW_EMAIL)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, "member@example.com")

    def test_confirm_page_warns_the_reader_to_continue_only_if_they_asked(self):
        self.assertContains(self.client.get(self.link(), follow=True), "Only continue if you asked for this change")

    def test_invalid_link_does_not_reveal_the_address(self):
        link = self.link()
        response = self.client.get(link[:-3] + "xx/")
        self.assertEqual(response.status_code, 400)
        self.assertNotContains(response, NEW_EMAIL, status_code=400)

    def test_confirming_needs_a_csrf_token(self):
        link = self.link()
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(self.member)
        review = client.get(link, follow=True).redirect_chain[-1][0]
        self.assertEqual(client.post(review, {"current_password": PASSWORD}).status_code, 403)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, "member@example.com")
        response = client.post(review, {"current_password": PASSWORD,
                                        "csrfmiddlewaretoken": client.cookies["csrftoken"].value})
        self.assertEqual(response.status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, NEW_EMAIL)

    def test_the_link_itself_cannot_be_posted(self):
        """The token URL only moves the token into the session; the change needs the review page."""
        self.assertEqual(self.client.post(self.link()).status_code, 405)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, "member@example.com")

    def test_changing_address_keeps_staff_role_flags_and_groups(self):
        admin = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        groups = set(admin.groups.values_list("name", flat=True))
        self.client.force_login(admin)
        self.confirm(self.link())
        admin.refresh_from_db()
        self.assertEqual(admin.email, NEW_EMAIL)
        self.assertEqual((admin.role, admin.status), (User.Role.SUPER_ADMIN, User.Status.ACTIVE))
        self.assertTrue(admin.is_staff and admin.is_superuser and admin.email_verified)
        self.assertEqual(set(admin.groups.values_list("name", flat=True)), groups)

    def test_confirming_switches_the_sign_in_address_and_stays_verified(self):
        link = self.link()
        self.assertEqual(self.confirm(link).status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, NEW_EMAIL)
        self.assertTrue(self.member.email_verified)
        self.assertFalse(PendingEmailChange.objects.exists())

        self.client.logout()
        self.client.post(LOGIN, {"username": "member@example.com", "password": PASSWORD})
        self.assertFalse(get_user(self.client).is_authenticated)
        self.client.post(LOGIN, {"username": NEW_EMAIL, "password": PASSWORD})
        self.assertEqual(get_user(self.client).pk, self.member.pk)

    def test_previous_address_is_told_without_the_new_one(self):
        self.confirm(self.link())
        notice = [message for message in mail.outbox if "member@example.com" in message.to][-1]
        self.assertIn("was changed", notice.subject)
        self.assertNotIn(NEW_EMAIL, notice.body)

    def test_audit_records_hashes_only(self):
        self.confirm(self.link())
        event = events("account.email_changed").get()
        self.assertEqual(event.actor_id, self.member.pk)
        self.assertEqual(set(event.metadata), {"previous_address_hash", "new_address_hash"})
        self.assertNotIn(NEW_EMAIL, str(event.metadata))
        self.assertNotIn("member@example.com", str(event.metadata))

    def test_link_works_once(self):
        link = self.link()
        self.confirm(link)
        self.assertEqual(self.confirm(link).status_code, 400)
        self.assertEqual(self.client.get(link).status_code, 400)

    def test_a_signed_out_reader_must_sign_in_as_the_owner_first(self):
        link = self.link()
        self.client.logout()
        review = self.client.get(link)
        self.assertEqual(review.status_code, 302)
        self.assertNotIn(link.rstrip("/").rsplit("/", 1)[-1], review.url)  # The token never reaches a later URL.
        to_login = self.client.get(review.url)
        self.assertRedirects(to_login, f"{LOGIN}?next={review.url}", fetch_redirect_response=False)
        self.client.post(LOGIN, {"username": "member@example.com", "password": PASSWORD})  # Session data survives.
        self.assertEqual(self.client.post(review.url, {"current_password": PASSWORD}).status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, NEW_EMAIL)

    def test_another_signed_in_account_is_refused_and_the_owner_keeps_the_link(self):
        link = self.link()
        other = make_user("other@example.com")
        self.client.force_login(other)
        response = self.confirm(link)
        self.assertEqual(response.status_code, 400)
        self.assertNotContains(response, NEW_EMAIL, status_code=400)
        other.refresh_from_db()
        self.member.refresh_from_db()
        self.assertEqual((other.email, self.member.email), ("other@example.com", "member@example.com"))
        denied = events("account.email_change.denied").get()
        self.assertEqual((denied.target_id, denied.metadata["reason"]), (str(other.pk), "other_account"))
        self.client.force_login(self.member)
        self.assertEqual(self.confirm(link).status_code, 200)  # Still usable by its owner.

    def test_expired_link_is_refused_and_not_shown_as_waiting(self):
        link = self.link()
        with self.settings(ACCOUNTS_EMAIL_VERIFICATION_TIMEOUT=-1):
            self.assertEqual(self.confirm(link).status_code, 400)
            self.assertNotContains(self.client.get(PROFILE), NEW_EMAIL)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, "member@example.com")

    def test_forged_or_cross_account_tokens_are_refused(self):
        link = self.link()
        change = PendingEmailChange.objects.get()
        bad_paths = [link[:-3] + "xx/",
                     reverse("accounts:email_change_confirm",
                             kwargs={"pk": change.pk, "token": verification_token_generator.make_token(self.member)})]
        other_client = self.client_class()
        other_client.force_login(make_user("other@example.com"))
        other_client.post(EMAIL_CHANGE, {"new_email": "x@example.com", "current_password": PASSWORD})
        other_token = link_sent_to("x@example.com").rstrip("/").rsplit("/", 1)[-1]
        bad_paths.append(reverse("accounts:email_change_confirm", kwargs={"pk": change.pk, "token": other_token}))
        for path in bad_paths:
            with self.subTest(path=path):
                self.assertEqual(self.confirm(path).status_code, 400)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, "member@example.com")

    def test_review_page_needs_the_link_opened_in_this_browser(self):
        self.link()
        change = PendingEmailChange.objects.get()
        review = reverse("accounts:email_change_review", kwargs={"pk": change.pk})
        self.assertEqual(self.client.get(review).status_code, 400)
        self.assertEqual(self.client.post(review, {"current_password": PASSWORD}).status_code, 400)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, "member@example.com")

    def test_new_request_ends_the_earlier_link(self):
        first = self.link()
        second = self.link("second@example.com")
        self.assertEqual(self.confirm(first).status_code, 400)
        self.assertEqual(self.confirm(second).status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, "second@example.com")

    def test_cancelling_ends_the_link_and_is_post_only(self):
        link = self.link()
        self.assertEqual(self.client.get(CANCEL).status_code, 405)
        self.assertTrue(PendingEmailChange.objects.exists())
        self.assertRedirects(self.client.post(CANCEL), PROFILE)
        self.assertEqual(self.confirm(link).status_code, 400)
        self.assertEqual(events("account.email_change.cancelled").count(), 1)

    def test_cancelling_touches_only_the_signed_in_account(self):
        self.link()
        self.client.force_login(make_user("other@example.com"))
        self.client.post(CANCEL)
        self.assertTrue(PendingEmailChange.objects.filter(user=self.member).exists())
        self.assertFalse(events("account.email_change.cancelled").exists())

    def test_password_change_ends_the_link(self):
        link = self.link()
        self.client.post(PASSWORD_CHANGE, {"old_password": PASSWORD, "new_password1": NEW_PASSWORD,
                                           "new_password2": NEW_PASSWORD})
        self.assertFalse(PendingEmailChange.objects.exists())
        self.assertEqual(self.confirm(link, password=NEW_PASSWORD).status_code, 400)

    def test_password_reset_ends_the_link(self):
        link = self.link()
        self.client.logout()
        self.client.post(reverse("accounts:password_reset"), {"email": "member@example.com"})
        reset = self.client.get(link_sent_to("member@example.com"), follow=True)
        self.client.post(reset.redirect_chain[-1][0], {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
        self.assertTrue(User.objects.get(pk=self.member.pk).check_password(NEW_PASSWORD))
        self.assertFalse(PendingEmailChange.objects.exists())
        self.client.force_login(User.objects.get(pk=self.member.pk))
        self.assertEqual(self.confirm(link, password=NEW_PASSWORD).status_code, 400)

    def test_link_expires_exactly_at_its_lifetime(self):
        self.request_change()
        change = PendingEmailChange.objects.get()
        generator = email_change_token_generator
        lifetime = settings.ACCOUNTS_EMAIL_VERIFICATION_TIMEOUT
        now = generator._num_seconds(generator._now())
        token_aged = lambda age: generator._make_token_with_timestamp(change, now - age, generator.secret)
        self.assertTrue(generator.check_token(change, token_aged(lifetime - 60)))
        self.assertFalse(generator.check_token(change, token_aged(lifetime + 60)))
        path = reverse("accounts:email_change_confirm", kwargs={"pk": change.pk, "token": token_aged(lifetime + 60)})
        self.assertEqual(self.confirm(path).status_code, 400)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, "member@example.com")

    def test_delayed_delivery_does_not_extend_the_link(self):
        client = Client()  # Unlike self.client, the worker does not run after its requests.
        client.force_login(self.member)
        client.post(EMAIL_CHANGE, {"new_email": NEW_EMAIL, "current_password": PASSWORD})
        OutboundEmail.objects.filter(recipient=NEW_EMAIL).update(
            token_issued_at=timezone.now() - timedelta(seconds=settings.ACCOUNTS_EMAIL_VERIFICATION_TIMEOUT + 60))
        self.client.get(PROFILE)  # This client runs the worker: the email goes out late.
        self.assertEqual(self.confirm(link_sent_to(NEW_EMAIL)).status_code, 400)

    def test_link_is_dead_once_the_password_hash_changes_even_with_the_row_present(self):
        link = self.link()
        self.member.set_password(NEW_PASSWORD)
        self.member.save()
        self.client.force_login(self.member)
        self.assertEqual(self.confirm(link, password=NEW_PASSWORD).status_code, 400)

    def test_address_taken_after_the_request_refuses_the_link(self):
        link = self.link()
        make_user(NEW_EMAIL)
        self.assertEqual(self.confirm(link).status_code, 400)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, "member@example.com")
        self.assertFalse(PendingEmailChange.objects.exists())

    def test_database_still_refuses_a_duplicate_address(self):
        make_user("taken@example.com")
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=self.member.pk).update(email="taken@example.com")

    def test_suspended_account_cannot_complete_a_change(self):
        link = self.link()
        self.member.status = User.Status.SUSPENDED
        self.member.save()
        self.client.logout()
        response = self.client.get(link)  # A suspended account's token no longer verifies.
        self.assertEqual(response.status_code, 400)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, "member@example.com")

    def test_unsent_link_is_withdrawn_when_the_request_is_cancelled(self):
        client = Client()  # Unlike self.client, the worker does not run after its requests.
        client.force_login(self.member)
        client.post(EMAIL_CHANGE, {"new_email": NEW_EMAIL, "current_password": PASSWORD})
        client.post(CANCEL)
        self.client.get(PROFILE)  # This client runs the worker.
        self.assertIsNone(link_sent_to(NEW_EMAIL))

    def test_earlier_reset_link_stops_working_after_the_change(self):
        self.client.logout()
        self.client.post(reverse("accounts:password_reset"), {"email": "member@example.com"})
        reset = link_sent_to("member@example.com")
        self.assertEqual(self.client.get(reset).status_code, 302)  # Valid: moves the token into the session.
        self.client.force_login(self.member)
        self.confirm(self.link())
        self.client.logout()
        self.assertFalse(self.client.get(reset, follow=True).context["validlink"])


class EmailChangeHardeningTests(SelfServiceTestCase):
    """RG-03: signed-in owner, fresh password, other sessions revoked, token kept out of URLs."""

    def link(self, new_email=NEW_EMAIL):
        self.request_change(new_email)
        return link_sent_to(new_email)

    def make_stale(self, seconds=None):
        session = self.client.session
        age = (seconds if seconds is not None else settings.ACCOUNTS_REAUTHENTICATION_SECONDS) + 1
        session[reauth.SESSION_KEY]["at"] -= age
        session.save()

    def test_a_fresh_password_from_the_request_needs_no_second_entry(self):
        review = self.client.get(self.link(), follow=True)
        self.assertNotContains(review, 'name="current_password"')
        self.assertEqual(self.client.post(review.redirect_chain[-1][0], {}).status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, NEW_EMAIL)

    def test_a_stale_confirmation_asks_for_the_password_again(self):
        link = self.link()
        self.make_stale()
        review = self.client.get(link, follow=True)
        self.assertContains(review, 'name="current_password"')
        url = review.redirect_chain[-1][0]
        self.assertContains(self.client.post(url, {}), "This field is required")
        self.assertContains(self.client.post(url, {"current_password": "wrong-password-123"}),
                            "Your current password was entered incorrectly")
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, "member@example.com")
        self.assertEqual(self.client.post(url, {"current_password": PASSWORD}).status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, NEW_EMAIL)

    def test_freshness_boundary(self):
        link = self.link()
        self.make_stale(seconds=settings.ACCOUNTS_REAUTHENTICATION_SECONDS - 2)  # Just inside the window.
        self.assertNotContains(self.client.get(link, follow=True), 'name="current_password"')
        self.make_stale(seconds=2)  # Now just past it.
        self.assertContains(self.client.get(link, follow=True), 'name="current_password"')

    def test_signing_in_alone_is_not_a_recent_confirmation(self):
        link = self.link()
        other_browser = self.client_class()
        other_browser.post(LOGIN, {"username": "member@example.com", "password": PASSWORD})
        self.assertContains(other_browser.get(link, follow=True), 'name="current_password"')

    def test_wrong_passwords_are_limited_and_audited(self):
        link = self.link()
        self.make_stale()
        url = self.client.get(link, follow=True).redirect_chain[-1][0]
        for _ in range(5):
            self.client.post(url, {"current_password": "wrong-password-123"})
        self.assertContains(self.client.post(url, {"current_password": PASSWORD}), "Too many incorrect passwords")
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, "member@example.com")
        self.assertTrue(events("auth.current_password.failed").filter(
            metadata__operation="email_change_confirm").exists())

    def test_other_sessions_are_signed_out_and_this_one_continues(self):
        phone = self.client_class()
        phone.force_login(self.member)
        self.assertEqual(phone.get(PROFILE).status_code, 200)
        self.confirm(self.link())
        self.assertNotEqual(phone.get(PROFILE).status_code, 200)
        self.assertFalse(get_user(phone).is_authenticated)
        self.assertEqual(self.client.get(PROFILE).status_code, 200)
        self.assertEqual(get_user(self.client).pk, self.member.pk)

    def test_a_password_change_still_ends_other_sessions(self):
        phone = self.client_class()
        phone.force_login(self.member)
        self.client.post(PASSWORD_CHANGE, {"old_password": PASSWORD, "new_password1": NEW_PASSWORD,
                                           "new_password2": NEW_PASSWORD})
        self.assertFalse(get_user(phone).is_authenticated)
        self.assertTrue(get_user(self.client).is_authenticated)

    def test_token_stays_out_of_later_urls_pages_and_headers(self):
        link = self.link()
        token = link.rstrip("/").rsplit("/", 1)[-1]
        opened = self.client.get(link)
        self.assertEqual(opened.status_code, 302)
        self.assertNotIn(token, opened.url)
        self.assertEqual(opened["Referrer-Policy"], "no-referrer")
        self.assertIn("no-store", opened["Cache-Control"])
        review = self.client.get(opened.url)
        self.assertNotContains(review, token)
        self.assertEqual(review["Referrer-Policy"], "no-referrer")
        done = self.client.post(opened.url, {"current_password": PASSWORD})
        self.assertNotContains(done, token)
        self.assertNotIn(views.EMAIL_CHANGE_SESSION_KEY, self.client.session)

    def test_other_token_links_send_no_referrer(self):
        self.client.logout()
        self.client.post(reverse("accounts:password_reset"), {"email": "member@example.com"})
        reset = self.client.get(link_sent_to("member@example.com"))
        self.assertEqual(reset["Referrer-Policy"], "no-referrer")
        invalid = self.client.get(reverse("accounts:confirm_email", kwargs={"uidb64": "MTIz", "token": "x-y"}))
        self.assertEqual(invalid["Referrer-Policy"], "no-referrer")

    def test_a_link_opened_for_one_change_cannot_complete_another(self):
        self.link()
        first = PendingEmailChange.objects.get()
        stale_link = link_sent_to(NEW_EMAIL)
        self.client.get(stale_link)  # Token for the first request is now in the session.
        self.link("second@example.com")  # Replaces the request: a new row and token.
        second = PendingEmailChange.objects.get()
        self.assertNotEqual(first.pk, second.pk)
        review = reverse("accounts:email_change_review", kwargs={"pk": second.pk})
        self.assertEqual(self.client.post(review, {"current_password": PASSWORD}).status_code, 400)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, "member@example.com")


@DEMO
class RestrictedDemoTests(AccountsTestCase):
    def setUp(self):
        super().setUp()
        self.demo = make_user("demo@example.com", verified=False)
        self.client.force_login(self.demo)

    def test_waived_account_uses_its_profile_but_cannot_change_address(self):
        self.assertEqual(self.client.get(PROFILE).status_code, 200)
        self.assertNotContains(self.client.get(PROFILE), EMAIL_CHANGE)
        self.assertRedirects(self.client.get(EMAIL_CHANGE), PROFILE)
        self.assertRedirects(
            self.client.post(EMAIL_CHANGE, {"new_email": NEW_EMAIL, "current_password": PASSWORD}), PROFILE)
        self.assertFalse(PendingEmailChange.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_waived_account_stays_unverified_after_a_profile_edit(self):
        self.client.post(EDIT, {"first_name": "Demo", "last_name": "", "phone_number": ""})
        self.demo.refresh_from_db()
        self.assertEqual(self.demo.first_name, "Demo")
        self.assertFalse(self.demo.email_verified)

    def test_unlisted_unverified_account_is_still_redirected(self):
        self.client.force_login(make_user("other@example.com", verified=False))
        self.assertRedirects(self.client.get(EDIT), VERIFY)
        self.assertRedirects(self.client.get(EMAIL_CHANGE), VERIFY)

    def test_confirmed_account_can_change_address_in_the_demo(self):
        self.client.force_login(make_user("confirmed@example.com"))
        self.assertRedirects(self.client.post(EMAIL_CHANGE, {"new_email": NEW_EMAIL, "current_password": PASSWORD}),
                             PROFILE)
        self.assertTrue(PendingEmailChange.objects.exists())

    def test_registration_and_money_stay_closed(self):
        self.assertFalse(features.is_enabled("registration"))
        self.assertEqual([key for key in features.FINANCIAL_FEATURES if features.is_enabled(key)], [])

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.dummy.EmailBackend")
    def test_closed_when_links_could_never_be_delivered(self):
        self.client.force_login(make_user("confirmed@example.com"))
        self.assertNotContains(self.client.get(PROFILE), EMAIL_CHANGE)
        self.assertRedirects(self.client.get(EMAIL_CHANGE), PROFILE)


class SecurityInformationTests(SelfServiceTestCase):
    def test_lists_own_activity_as_labels_and_times_only(self):
        self.client.logout()
        self.client.post(LOGIN, {"username": "member@example.com", "password": "wrong-password-123"})
        self.client.post(LOGIN, {"username": "member@example.com", "password": PASSWORD})
        response = self.client.get(PROFILE)
        texts = [item["text"] for item in response.context["activity"]]
        self.assertEqual([str(text) for text in texts], ["Signed in", "Failed sign-in attempt"])
        self.assertNotIn("ip", response.context["activity"][0])
        self.assertContains(response, "Recent security activity")

    def test_never_shows_another_accounts_activity(self):
        other = make_user("other@example.com")
        client = self.client_class()
        client.post(LOGIN, {"username": other.email, "password": PASSWORD})
        self.assertEqual(self.client.get(PROFILE).context["activity"], [])

    def test_a_sign_in_time_is_shown(self):
        self.client.logout()
        self.client.post(LOGIN, {"username": "member@example.com", "password": PASSWORD})
        self.assertContains(self.client.get(PROFILE), "Last sign-in")


class NavigationTests(SelfServiceTestCase):
    def test_account_pages_are_linked_from_the_hub_and_marked_current(self):
        page = self.client.get(PROFILE)
        for url in (EDIT, EMAIL_CHANGE, PASSWORD_CHANGE):
            self.assertContains(page, f'href="{url}"')
        for url in (EDIT, EMAIL_CHANGE):
            self.assertContains(self.client.get(url), f'aria-current="page" href="{PROFILE}"')

    def test_console_links_to_the_account_pages(self):
        self.client.force_login(make_user("root@example.com", role=User.Role.SUPER_ADMIN))
        console = self.client.get(reverse("administration:overview"))
        self.assertContains(console, f'class="app-nav__link" href="{PROFILE}"')
        self.assertEqual(self.client.get(PROFILE).status_code, 200)


class FinancialSafeguardTests(SelfServiceTestCase):
    def counts(self):
        return {model._meta.label: model.objects.count()
                for label in ("wallets", "withdrawals", "payments", "rewards", "memberships")
                for model in apps.get_app_config(label).get_models()}

    def test_account_changes_write_nothing_financial(self):
        before = self.counts()
        self.client.post(EDIT, {"first_name": "A", "last_name": "B", "phone_number": "+255712345678"})
        self.request_change()
        self.confirm(link_sent_to(NEW_EMAIL))
        self.assertEqual(self.counts(), before)

    def test_token_cannot_be_derived_from_a_verification_token(self):
        self.request_change()
        change = PendingEmailChange.objects.get()
        self.assertFalse(email_change_token_generator.check_token(
            change, verification_token_generator.make_token(self.member)))
