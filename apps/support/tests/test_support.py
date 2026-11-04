"""Phase 15-B: customer support tickets (workflow, isolation, staff workspace,
notifications, audit, database integrity)."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, InternalError, connection, transaction
from django.db.utils import DatabaseError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.helpers import AccountsTestCase, User, make_user
from apps.advertising.models import AdvertiserMembership, Organization
from apps.audit.models import AuditEvent
from apps.notifications.models import Notification, OutboundEmail
from apps.support import services
from apps.support.forms import new_submission_key
from apps.support.models import Ticket, TicketEvent, TicketMessage

S = Ticket.Status
SECRET_NOTE = "INTERNAL_NOTE_Zq81"


def grant(user, *codenames):
    user.user_permissions.add(*Permission.objects.filter(content_type__app_label="support", codename__in=codenames))
    return User.objects.get(pk=user.pk)  # Drop the cached permissions.


class SupportTestCase(AccountsTestCase):
    def setUp(self):
        super().setUp()
        self.member = make_user("member@example.com")
        self.other = make_user("other@example.com")
        self.agent = grant(make_user("agent@example.com", role=User.Role.CONTENT_MANAGER),
                           "view_tickets", "manage_tickets")
        self.agent2 = grant(make_user("agent2@example.com", role=User.Role.CONTENT_MANAGER), "manage_tickets")
        self.viewer = grant(make_user("viewer@example.com", role=User.Role.CONTENT_MANAGER), "view_tickets")
        self.staff = make_user("staff@example.com", role=User.Role.CONTENT_MANAGER)  # No support permission.

    def open_ticket(self, user=None, **values):
        defaults = {"subject": "Cannot watch videos", "category": Ticket.Category.VIDEOS,
                    "description": "The player stays black on my phone.", "submission_key": new_submission_key()}
        return services.create_ticket(user or self.member, **{**defaults, **values})

    def reply(self, user, ticket, body="More details here", **values):
        return services.customer_reply(user, ticket, body=body, submission_key=new_submission_key(), **values)

    def staff_reply(self, ticket, body="We are looking into it", internal=False, then_status="", actor=None):
        return services.staff_reply(actor or self.agent, ticket, body=body, internal=internal,
                                    submission_key=new_submission_key(), then_status=then_status)

    def fresh(self, ticket):
        return Ticket.objects.get(pk=ticket.pk)

    def org_with(self, owner, *managers, name="Brand"):
        org = Organization.objects.create(display_name=name, legal_name=f"{name} Limited",
                                          contact_email="ads@example.com", created_by=owner)
        AdvertiserMembership.objects.create(organization=org, user=owner, role=AdvertiserMembership.Role.OWNER)
        for manager in managers:
            AdvertiserMembership.objects.create(organization=org, user=manager,
                                                role=AdvertiserMembership.Role.MANAGER)
        return org


class TicketCreationTests(SupportTestCase):
    def test_member_opens_a_ticket_with_reference_message_and_history(self):
        ticket = self.open_ticket()
        self.assertRegex(ticket.reference, r"^CS-[A-HJ-NP-Z2-9]{8}$")
        self.assertEqual((ticket.status, ticket.priority, ticket.assignee), (S.OPEN, "NORMAL", None))
        self.assertEqual(ticket.messages.get().body, "The player stays black on my phone.")
        self.assertEqual(list(ticket.events.values_list("action", flat=True)), ["CREATED"])
        audit = AuditEvent.objects.get(action="support.ticket.created")
        self.assertEqual(audit.category, "SUPPORT")
        self.assertNotIn("player", str(audit.metadata))  # No message text in the audit trail.

    def test_validation(self):
        for values, field in [({"subject": "Hi"}, "subject"), ({"description": "short"}, "description"),
                              ({"category": "MONEY"}, "category"), ({"subject": "x" * 151}, "subject"),
                              ({"description": "x" * 5001}, "description")]:
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.open_ticket(**values)
        with self.assertRaises(ValidationError):
            self.open_ticket(submission_key="bad key!")
        self.assertFalse(Ticket.objects.exists())

    def test_same_form_submitted_twice_creates_one_ticket(self):
        key = new_submission_key()
        first = self.open_ticket(submission_key=key)
        second = self.open_ticket(submission_key=key)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(Notification.objects.filter(kind="support.received").count(), 1)

    def test_only_active_members_may_open_tickets(self):
        pending = make_user("pending@example.com", status=User.Status.PENDING)
        for user in (pending, self.agent, self.staff):
            with self.subTest(user=user.email), self.assertRaises(services.NotAuthorized):
                self.open_ticket(user=user)
        self.assertTrue(AuditEvent.objects.filter(action="support.action.denied").exists())

    def test_unverified_email_may_still_ask_for_help(self):
        unverified = make_user("unverified@example.com", verified=False)
        self.assertEqual(self.open_ticket(user=unverified).status, S.OPEN)

    def test_ticket_creation_is_rate_limited(self):
        for _ in range(5):
            self.open_ticket()
        with self.assertRaises(services.RateLimited):
            self.open_ticket()
        self.assertEqual(Ticket.objects.count(), 5)

    def test_reference_collision_is_retried(self):
        first = self.open_ticket()
        with mock.patch("apps.support.services._new_reference", side_effect=[first.reference, "CS-ZZZZZZZZ"]):
            second = self.open_ticket()
        self.assertEqual(second.reference, "CS-ZZZZZZZZ")

    def test_organization_must_be_the_customers_own(self):
        owner = make_user("owner@brand.example")
        org = self.org_with(owner)
        with self.assertRaises(services.NotAuthorized):
            self.open_ticket(organization=org)  # self.member is not on the team.
        self.assertEqual(self.open_ticket(user=owner, organization=org).organization, org)


class IsolationTests(SupportTestCase):
    def test_customers_see_only_their_own_tickets(self):
        mine = self.open_ticket()
        theirs = self.open_ticket(user=self.other, subject="Someone else's problem")
        self.assertEqual(list(services.visible_to_customer(self.member)), [mine])
        with self.assertRaises(services.NotFound):
            services.ticket_for_customer(self.member, theirs.reference)
        with self.assertRaises(services.NotFound):
            self.reply(self.member, theirs)

    def test_organization_visibility_owner_manager_and_removed_member(self):
        owner = make_user("owner@brand.example")
        manager = make_user("manager@brand.example")
        manager2 = make_user("manager2@brand.example")
        org = self.org_with(owner, manager, manager2)
        by_manager = self.open_ticket(user=manager, organization=org)
        by_owner = self.open_ticket(user=owner, organization=org, subject="Owner question")
        self.assertCountEqual(services.visible_to_customer(owner), [by_manager, by_owner])  # Owners see all.
        self.assertEqual(list(services.visible_to_customer(manager)), [by_manager])  # Managers: their own.
        self.assertEqual(list(services.visible_to_customer(manager2)), [])
        AdvertiserMembership.objects.filter(user=manager).update(is_active=False, removed_at=timezone.now())
        self.assertEqual(list(services.visible_to_customer(manager)), [])  # Left the team: no access.
        self.assertCountEqual(services.visible_to_customer(owner), [by_manager, by_owner])
        other_org_owner = make_user("owner@other.example")
        self.org_with(other_org_owner, name="Other brand")
        self.assertEqual(list(services.visible_to_customer(other_org_owner)), [])

    def test_other_customers_ticket_is_404_over_http(self):
        theirs = self.open_ticket(user=self.other)
        self.client.force_login(self.member)
        url = reverse("support:ticket", args=[theirs.reference])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {"body": "hello there", "submission_key": new_submission_key()})
                         .status_code, 404)
        self.assertEqual(theirs.messages.count(), 1)

    def test_customer_pages_never_show_internal_notes_or_staff_identities(self):
        ticket = self.open_ticket()
        services.assign(self.agent, ticket, self.agent)
        self.staff_reply(ticket, body=SECRET_NOTE, internal=True)
        self.staff_reply(ticket, body="Please clear the cache.")
        self.client.force_login(self.member)
        page = self.client.get(reverse("support:ticket", args=[ticket.reference])).content.decode()
        self.assertIn("Please clear the cache.", page)
        self.assertNotIn(SECRET_NOTE, page)
        self.assertNotIn("agent@example.com", page)
        self.assertIn("ClipCash support", page)
        for notice in Notification.objects.filter(recipient=self.member):
            self.assertNotIn(SECRET_NOTE, notice.title + notice.message)
            self.assertNotIn("clear the cache", notice.title + notice.message)  # No message text in notices.
        for email in OutboundEmail.objects.all():
            self.assertNotIn(SECRET_NOTE, str(email.__dict__))

    def test_customer_cannot_change_internal_state_through_the_reply_form(self):
        ticket = self.open_ticket()
        self.client.force_login(self.member)
        self.client.post(reverse("support:ticket", args=[ticket.reference]), {
            "body": "Reply text", "submission_key": new_submission_key(), "internal": "on", "status": "CLOSED",
            "priority": "URGENT", "assignee": str(self.member.pk), "then_status": "RESOLVED"})
        ticket = self.fresh(ticket)
        self.assertEqual((ticket.status, ticket.priority, ticket.assignee_id), (S.OPEN, "NORMAL", None))
        self.assertFalse(ticket.messages.filter(internal=True).exists())

    def test_customers_cannot_use_the_staff_workspace_or_actions(self):
        ticket = self.open_ticket()
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("administration:support")).status_code, 302)  # To admin login.
        response = self.client.post(reverse("administration:support_ticket", args=[ticket.reference]),
                                    {"action": "status", "status": "CLOSED", "expected_status": "OPEN"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.fresh(ticket).status, S.OPEN)
        with self.assertRaises(services.NotAuthorized):
            services.set_status(self.member, ticket, S.CLOSED, expected_status=S.OPEN)
        with self.assertRaises(services.NotAuthorized):
            services.assign(self.member, ticket, self.member)


class ConversationTests(SupportTestCase):
    def test_staff_reply_then_waiting_and_customer_reply_returns_to_the_team(self):
        ticket = self.open_ticket()
        services.assign(self.agent, ticket, self.agent)
        self.assertEqual(self.fresh(ticket).status, S.IN_PROGRESS)
        self.staff_reply(ticket, then_status=S.WAITING)
        self.assertEqual(self.fresh(ticket).status, S.WAITING)
        self.reply(self.member, ticket)
        self.assertEqual(self.fresh(ticket).status, S.IN_PROGRESS)  # Assigned: back in progress.

    def test_customer_reply_on_unassigned_waiting_ticket_returns_it_to_open(self):
        ticket = self.open_ticket()
        services.set_status(self.agent, ticket, S.WAITING, expected_status=S.OPEN)
        self.reply(self.member, ticket)
        self.assertEqual(self.fresh(ticket).status, S.OPEN)

    def test_reply_on_resolved_ticket_reopens_within_window_only(self):
        ticket = self.open_ticket()
        self.staff_reply(ticket, then_status=S.RESOLVED)
        self.assertIsNotNone(self.fresh(ticket).resolved_at)
        self.reply(self.member, ticket, body="Still broken")
        ticket = self.fresh(ticket)
        self.assertEqual((ticket.status, ticket.resolved_at), (S.OPEN, None))
        self.assertTrue(ticket.events.filter(action=TicketEvent.Action.REOPENED).exists())
        self.assertEqual(Notification.objects.filter(kind="support.reopened").count(), 2)  # Both agents.

        services.set_status(self.agent, ticket, S.RESOLVED, expected_status=S.OPEN)
        later = timezone.now() + timedelta(days=15)
        with mock.patch("apps.support.services.timezone.now", return_value=later):
            with self.assertRaises(services.InvalidState):
                self.reply(self.member, ticket)
        self.assertEqual(self.fresh(ticket).status, S.RESOLVED)

    def test_closed_ticket_takes_no_replies_or_changes(self):
        ticket = self.open_ticket()
        services.set_status(self.agent, ticket, S.CLOSED, expected_status=S.OPEN)
        with self.assertRaises(services.InvalidState):
            self.reply(self.member, ticket)
        with self.assertRaises(services.InvalidState):
            self.staff_reply(ticket)
        with self.assertRaises(services.InvalidState):
            services.assign(self.agent, ticket, self.agent)
        with self.assertRaises(services.InvalidState):
            services.set_status(self.agent, ticket, S.OPEN, expected_status=S.CLOSED)
        self.assertIsNotNone(self.fresh(ticket).closed_at)

    def test_repeated_reply_is_stored_once(self):
        ticket = self.open_ticket()
        key = new_submission_key()
        services.customer_reply(self.member, ticket, body="Twice", submission_key=key)
        services.customer_reply(self.member, ticket, body="Twice", submission_key=key)
        self.assertEqual(ticket.messages.count(), 2)  # Description + one reply.
        self.assertEqual(Notification.objects.filter(kind="support.customer_replied").count(), 2)  # Two agents.

    def test_replies_are_rate_limited(self):
        ticket = self.open_ticket()
        with mock.patch("apps.support.services.REPLIES_PER_HOUR") as throttle:
            throttle.is_limited.return_value = True
            with self.assertRaises(services.RateLimited):
                self.reply(self.member, ticket)

    def test_internal_note_needs_no_status_change_and_is_not_notified_to_customer(self):
        ticket = self.open_ticket()
        before = Notification.objects.filter(recipient=self.member).count()
        self.staff_reply(ticket, body=SECRET_NOTE, internal=True)
        with self.assertRaises(ValidationError):
            self.staff_reply(ticket, body="note", internal=True, then_status=S.RESOLVED)
        self.assertEqual(Notification.objects.filter(recipient=self.member).count(), before)
        self.assertTrue(AuditEvent.objects.filter(action="support.ticket.note_added").exists())

    def test_customer_pages_flow_over_http(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("support:create"))
        key = response.context["form"].initial["submission_key"]
        response = self.client.post(reverse("support:create"), {
            "subject": "<script>alert(1)</script>", "category": "ACCOUNT",
            "description": "I cannot change my phone number.", "submission_key": key})
        ticket = Ticket.objects.get()
        self.assertRedirects(response, reverse("support:ticket", args=[ticket.reference]))
        page = self.client.get(reverse("support:tickets")).content.decode()
        self.assertIn(ticket.reference, page)
        self.assertNotIn("<script>alert(1)</script>", page)  # Escaped.
        self.assertIn("&lt;script&gt;", page)
        response = self.client.post(reverse("support:ticket", args=[ticket.reference]),
                                    {"body": "Any update?", "submission_key": new_submission_key()})
        self.assertRedirects(response, reverse("support:ticket", args=[ticket.reference]))
        self.assertEqual(ticket.messages.count(), 2)
        self.assertEqual(self.client.get(reverse("support:tickets"), {"q": ticket.reference.lower()})
                         .context["page"].paginator.count, 1)
        self.assertEqual(self.client.get(reverse("support:tickets"), {"status": "CLOSED"})
                         .context["page"].paginator.count, 0)

    def test_forms_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.member)
        response = client.post(reverse("support:create"), {
            "subject": "Hello there", "category": "OTHER", "description": "Something is wrong.",
            "submission_key": new_submission_key()})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Ticket.objects.exists())


class StaffWorkspaceTests(SupportTestCase):
    def test_queue_requires_support_permission(self):
        self.open_ticket()
        for user, expected in [(self.staff, 403), (self.viewer, 200), (self.agent, 200)]:
            self.client.force_login(user)
            with self.subTest(user=user.email):
                self.assertEqual(self.client.get(reverse("administration:support")).status_code, expected)

    def test_viewer_may_read_but_not_act(self):
        ticket = self.open_ticket()
        self.client.force_login(self.viewer)
        url = reverse("administration:support_ticket", args=[ticket.reference])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(url, {"action": "status", "status": "CLOSED",
                                                "expected_status": "OPEN"}).status_code, 403)
        self.assertEqual(self.fresh(ticket).status, S.OPEN)
        with self.assertRaises(services.NotAuthorized):
            self.staff_reply(ticket, actor=self.viewer)

    def test_staff_without_permission_cannot_open_a_ticket(self):
        ticket = self.open_ticket()
        self.client.force_login(self.staff)
        url = reverse("administration:support_ticket", args=[ticket.reference])
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_assignment_and_reassignment(self):
        ticket = self.open_ticket()
        services.assign(self.agent, ticket, self.agent2)
        ticket = self.fresh(ticket)
        self.assertEqual((ticket.assignee, ticket.status), (self.agent2, S.IN_PROGRESS))
        self.assertEqual(Notification.objects.filter(kind="support.assigned", recipient=self.agent2).count(), 1)
        services.assign(self.agent, ticket, self.agent)  # Self-assignment: no notice to oneself.
        self.assertFalse(Notification.objects.filter(kind="support.assigned", recipient=self.agent).exists())
        services.assign(self.agent, ticket, None)
        ticket = self.fresh(ticket)
        self.assertEqual((ticket.assignee, ticket.status), (None, S.OPEN))
        self.assertEqual(AuditEvent.objects.filter(action="support.ticket.assigned").count(), 3)

    def test_only_support_agents_can_be_assigned(self):
        ticket = self.open_ticket()
        for user in (self.member, self.staff, self.viewer):
            with self.subTest(user=user.email), self.assertRaises(ValidationError):
                services.assign(self.agent, ticket, user)

    def test_stale_status_change_is_refused(self):
        ticket = self.open_ticket()
        services.set_status(self.agent, ticket, S.WAITING, expected_status=S.OPEN)
        with self.assertRaises(services.InvalidState):
            services.set_status(self.agent2, ticket, S.RESOLVED, expected_status=S.OPEN)

    def test_invalid_transition_is_refused(self):
        ticket = self.open_ticket()
        services.set_status(self.agent, ticket, S.RESOLVED, expected_status=S.OPEN)
        with self.assertRaises(services.InvalidState):
            services.set_status(self.agent, ticket, S.WAITING, expected_status=S.RESOLVED)
        services.set_status(self.agent, ticket, S.IN_PROGRESS, expected_status=S.RESOLVED)  # Staff reopen.
        ticket = self.fresh(ticket)
        self.assertEqual((ticket.status, ticket.resolved_at), (S.IN_PROGRESS, None))

    def test_priority_change_is_recorded(self):
        ticket = self.open_ticket()
        services.set_priority(self.agent, ticket, "URGENT")
        self.assertEqual(self.fresh(ticket).priority, "URGENT")
        self.assertTrue(ticket.events.filter(action="PRIORITY", to_value="URGENT").exists())
        with self.assertRaises(ValidationError):
            services.set_priority(self.agent, ticket, "CRITICAL")

    def test_staff_detail_shows_notes_and_actions_work_over_http(self):
        ticket = self.open_ticket()
        self.staff_reply(ticket, body=SECRET_NOTE, internal=True)
        self.client.force_login(self.agent)
        url = reverse("administration:support_ticket", args=[ticket.reference])
        self.assertContains(self.client.get(url), SECRET_NOTE)
        self.client.post(url, {"action": "assign", "assignee": str(self.agent2.pk), "expected_status": "OPEN"})
        self.client.post(url, {"action": "priority", "priority": "HIGH"})
        self.client.post(url, {"action": "reply", "body": "Fixed now", "then_status": "RESOLVED",
                               "submission_key": new_submission_key()})
        ticket = self.fresh(ticket)
        self.assertEqual((ticket.assignee, ticket.priority, ticket.status), (self.agent2, "HIGH", S.RESOLVED))
        response = self.client.post(url, {"action": "assign", "assignee": "not-a-uuid", "expected_status": "RESOLVED"})
        self.assertEqual(response.status_code, 400)

    def test_queue_filters_search_and_pagination(self):
        first = self.open_ticket(category="ACCOUNT")
        second = self.open_ticket(user=self.other, category="TECHNICAL", subject="Page error on wallet")
        services.assign(self.agent, second, self.agent)
        services.set_status(self.agent, first, S.CLOSED, expected_status=S.OPEN)
        self.client.force_login(self.agent)
        queue = reverse("administration:support")

        def refs(**params):
            return [t.reference for t in self.client.get(queue, params).context["page"].object_list]

        self.assertEqual(refs(), [second.reference])  # Default: unresolved only.
        self.assertCountEqual(refs(status=""), [first.reference, second.reference])
        self.assertEqual(refs(status="", category="ACCOUNT"), [first.reference])
        self.assertEqual(refs(status="", assignee="me"), [second.reference])
        self.assertEqual(refs(status="", assignee="none"), [first.reference])
        self.assertEqual(refs(status="", q=first.reference.lower()), [first.reference])
        self.assertEqual(refs(status="", q="wallet"), [second.reference])
        self.assertEqual(refs(status="", assignee="garbage"), [])
        for _ in range(30):
            Ticket.objects.create(reference=services._new_reference(), requester=self.member, subject="Bulk",
                                  category="OTHER", submission_key=new_submission_key(),
                                  last_activity_at=timezone.now())
        page = self.client.get(queue).context["page"]
        self.assertEqual((page.paginator.num_pages, len(page.object_list)), (2, 25))

    def test_staff_visiting_customer_pages_are_sent_to_the_console(self):
        self.client.force_login(self.agent)
        self.assertRedirects(self.client.get(reverse("support:tickets")), reverse("administration:support"))
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("support:tickets")).status_code, 403)

    def test_support_role_grants_no_financial_or_admin_privileges(self):
        agent = User.objects.get(pk=self.agent.pk)
        for perm in ("wallets.view_wallet", "withdrawals.view_demowithdrawal", "payments.view_paymentintent",
                     "accounts.change_user", "audit.view_auditevent", "memberships.manage_memberships"):
            with self.subTest(perm=perm):
                self.assertFalse(agent.has_perm(perm))
        self.client.force_login(agent)
        self.assertEqual(self.client.get(reverse("administration:wallets")).status_code, 403)


class NotificationTests(SupportTestCase):
    def test_lifecycle_notifications_are_targeted_and_deduplicated(self):
        ticket = self.open_ticket()
        self.assertEqual(Notification.objects.get(recipient=self.member).kind, "support.received")
        self.assertCountEqual(Notification.objects.filter(kind="support.new_ticket").values_list("recipient", flat=True),
                              [self.agent.pk, self.agent2.pk])  # Viewer and plain staff are not agents.
        self.assertTrue(all(n.category == "SUPPORT" for n in Notification.objects.all()))

        services.assign(self.agent, ticket, self.agent2)
        self.reply(self.member, ticket)
        self.assertEqual(Notification.objects.filter(kind="support.customer_replied").get().recipient, self.agent2)

        self.staff_reply(ticket, actor=self.agent2)
        staff_reply = Notification.objects.get(kind="support.staff_replied")
        self.assertEqual(staff_reply.recipient, self.member)
        self.assertEqual(staff_reply.link, reverse("support:ticket", args=[ticket.reference]))

        services.set_status(self.agent2, ticket, S.RESOLVED, expected_status=S.IN_PROGRESS)
        services.set_status(self.agent2, ticket, S.CLOSED, expected_status=S.RESOLVED)
        self.assertEqual(Notification.objects.filter(recipient=self.member, kind="support.resolved").count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.member, kind="support.closed").count(), 1)
        self.assertEqual(OutboundEmail.objects.filter(user_id=self.member.pk).count(), 2)  # Reply + resolved.

    def test_staff_links_point_to_the_console(self):
        ticket = self.open_ticket()
        notice = Notification.objects.filter(kind="support.new_ticket").first()
        self.assertEqual(notice.link, reverse("administration:support_ticket", args=[ticket.reference]))


class DatabaseIntegrityTests(SupportTestCase):
    def assert_refused(self, statement, params):
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(statement, params)

    def test_tickets_cannot_be_deleted_or_change_identity(self):
        ticket = self.open_ticket()
        self.assert_refused("DELETE FROM support_ticket WHERE id = %s", [ticket.pk])
        self.assert_refused("UPDATE support_ticket SET requester_id = %s WHERE id = %s", [self.other.pk, ticket.pk])
        self.assert_refused("UPDATE support_ticket SET reference = 'CS-HACKED00' WHERE id = %s", [ticket.pk])

    def test_status_state_machine_is_enforced_by_postgres(self):
        ticket = self.open_ticket()
        services.set_status(self.agent, ticket, S.CLOSED, expected_status=S.OPEN)
        self.assert_refused("UPDATE support_ticket SET status = 'OPEN' WHERE id = %s", [ticket.pk])
        self.assert_refused("UPDATE support_ticket SET priority = 'LOW' WHERE id = %s", [ticket.pk])
        self.assert_refused("UPDATE support_ticket SET status = 'BOGUS' WHERE id = %s", [ticket.pk])
        with self.assertRaises((IntegrityError, InternalError, DatabaseError)), transaction.atomic():
            Ticket.objects.create(reference="CS-AAAAAAAA", requester=self.member, subject="Direct",
                                  category="OTHER", status=S.CLOSED, submission_key=new_submission_key(),
                                  last_activity_at=timezone.now())

    def test_messages_and_history_are_append_only(self):
        ticket = self.open_ticket()
        message = ticket.messages.get()
        event = ticket.events.get()
        self.assert_refused("UPDATE support_ticketmessage SET body = 'edited' WHERE id = %s", [message.pk])
        self.assert_refused("DELETE FROM support_ticketmessage WHERE id = %s", [message.pk])
        self.assert_refused("DELETE FROM support_ticketevent WHERE id = %s", [event.pk])

    def test_customer_cannot_write_internal_notes_even_directly(self):
        ticket = self.open_ticket()
        with self.assertRaises(IntegrityError), transaction.atomic():
            TicketMessage.objects.create(ticket=ticket, author=self.member, body="x", internal=True,
                                         from_staff=False, submission_key=new_submission_key())


class NavigationTests(SupportTestCase):
    def test_member_and_advertiser_sidebar_links_to_support(self):
        owner = make_user("owner@brand.example")
        self.org_with(owner)
        for user in (self.member, owner):
            self.client.force_login(user)
            with self.subTest(user=user.email):
                self.assertContains(self.client.get(reverse("dashboard")), f'href="{reverse("support:tickets")}"')
        page = self.client.get(reverse("support:tickets"))
        self.assertContains(page, 'aria-current="page" href="%s"' % reverse("support:tickets"))

    def test_console_sidebar_shows_support_queue_only_with_permission(self):
        queue = f'href="{reverse("administration:support")}"'
        for user, shown in [(self.agent, True), (self.viewer, True), (self.staff, False)]:
            self.client.force_login(user)
            with self.subTest(user=user.email):
                page = self.client.get(reverse("administration:overview")).content.decode()
                self.assertEqual(queue in page, shown)
