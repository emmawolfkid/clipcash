from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.utils import timezone

from apps.advertising import services
from apps.advertising.models import AdvertiserInvitation, AdvertiserMembership, Organization
from apps.audit.models import AuditEvent

from .helpers import AdvertisingTestCase, User, enable_advertisers, make_user

V = Organization.Verification


class OnboardingTests(AdvertisingTestCase):
    def test_application_creates_pending_org_with_owner(self):
        applicant = make_user("new@shop.example")
        org = self.make_org(applicant, "Shop")
        self.assertEqual(org.verification_status, V.PENDING)
        self.assertIsNotNone(org.submitted_at)
        membership = AdvertiserMembership.objects.get(organization=org)
        self.assertEqual((membership.user, membership.role), (applicant, "OWNER"))
        applicant.refresh_from_db()
        self.assertEqual((applicant.role, applicant.is_staff), (User.Role.MEMBER, False))  # Platform role untouched.
        self.assertTrue(AuditEvent.objects.filter(action="advertiser.created", target_id=str(org.pk)).exists())
        with self.assertRaises(services.InvalidState):
            self.make_org(applicant, "Shop Two")  # One pending application at a time.

    def test_ineligible_accounts_refused(self):
        for user, reason in ((make_user("unverified@x.example", verified=False), "email_unverified"),
                             (self.manager, "staff_account"), (self.root, "staff_account")):
            with self.subTest(reason=reason), self.assertRaises(services.NotAuthorized) as caught:
                self.make_org(user, f"Org {reason}")
            self.assertEqual(caught.exception.code, reason)
        self.assertTrue(AuditEvent.objects.filter(action="advertiser.action.denied").exists())
        enable_advertisers(False)
        with self.assertRaises(services.InvalidState):
            self.make_org(make_user("later@x.example"), "Later")

    def test_names_unique_and_validated(self):
        with self.assertRaises(ValidationError):
            self.make_org(make_user("dup@x.example"), "BRAND")
        with self.assertRaises(ValidationError):
            self.make_org(make_user("bad@x.example"), "Bad", country="Tanzania")


class VerificationTests(AdvertisingTestCase):
    def setUp(self):
        super().setUp()
        self.pending = self.make_org(make_user("p@x.example"), "Pending Co")

    def test_decisions_and_reasons(self):
        with self.assertRaises(ValidationError):
            services.review_organization(self.root, self.pending, "reject", expected_status="PENDING", reason="")
        org = services.review_organization(self.root, self.pending, "reject", expected_status="PENDING",
                                           reason="Missing business details")
        self.assertEqual((org.verification_status, org.reviewed_by, org.review_reason),
                         (V.REJECTED, self.root, "Missing business details"))
        owner = org.memberships.get().user
        org = services.resubmit_organization(owner, org)
        self.assertEqual((org.verification_status, org.reviewed_by), (V.PENDING, None))
        org = services.review_organization(self.root, org, "approve", expected_status="PENDING")
        org = services.review_organization(self.root, org, "suspend", expected_status="APPROVED", reason="Complaint")
        org = services.review_organization(self.root, org, "reinstate", expected_status="SUSPENDED")
        self.assertEqual(org.verification_status, V.APPROVED)
        actions = list(AuditEvent.objects.filter(target_id=str(org.pk), action__startswith="advertiser.verification")
                       .order_by("occurred_at").values_list("action", flat=True))
        self.assertEqual(actions, ["advertiser.verification.rejected", "advertiser.verification.approved",
                                   "advertiser.verification.suspended", "advertiser.verification.reinstated"])

    def test_stale_idempotent_and_invalid_decisions(self):
        services.review_organization(self.root, self.pending, "approve", expected_status="PENDING")
        # Same decision again (double submit): no error, no second event.
        services.review_organization(self.root, self.pending, "approve", expected_status="APPROVED")
        self.assertEqual(AuditEvent.objects.filter(action="advertiser.verification.approved",
                                                   target_id=str(self.pending.pk)).count(), 1)
        with self.assertRaises(services.StaleDecision):  # Reviewer saw PENDING; it is APPROVED now.
            services.review_organization(self.root, self.pending, "reject", expected_status="PENDING", reason="Late!")
        rejected = services.review_organization(self.root, self.make_org(make_user("r@x.example"), "Rejected Co"),
                                                "reject", expected_status="PENDING", reason="Incomplete")
        with self.assertRaises(services.InvalidState):  # Rejected cannot be suspended.
            services.review_organization(self.root, rejected, "suspend", expected_status="REJECTED", reason="Nope!")

    def test_only_reviewers_decide(self):
        for actor in (self.manager, self.member, self.owner):
            with self.subTest(actor=actor.email), self.assertRaises(services.NotAuthorized):
                services.review_organization(actor, self.pending, "approve", expected_status="PENDING")
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.verification_status, V.PENDING)

    def test_identity_fixed_after_approval(self):
        with self.assertRaises(ValidationError):
            services.update_organization(self.owner, self.org, {"legal_name": "Other Ltd"})
        org = services.update_organization(self.owner, self.org, {"contact_phone": "+255700000000"})
        self.assertEqual(org.contact_phone, "+255700000000")
        manager = make_user("mgr@brand.example")
        AdvertiserMembership.objects.create(organization=self.org, user=manager, role="MANAGER")
        with self.assertRaises(services.NotAuthorized):
            services.update_organization(manager, self.org, {"website": "https://x.example"})

    def test_database_guards(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            Organization.objects.create(display_name="Sneaky", legal_name="Sneaky Ltd", contact_email="a@b.example",
                                        verification_status=V.APPROVED, reviewed_at=timezone.now(),
                                        reviewed_by=self.root)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Organization.objects.filter(pk=self.pending.pk).update(verification_status=V.SUSPENDED,
                                                                   reviewed_at=timezone.now(), reviewed_by=self.root)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Organization.objects.filter(pk=self.pending.pk).delete()


class TeamTests(AdvertisingTestCase):
    def test_invitation_flow(self):
        invited = make_user("teammate@brand.example")
        services.invite_member(self.owner, self.org, "Teammate@Brand.example")
        with self.assertRaises(ValidationError):
            services.invite_member(self.owner, self.org, "teammate@brand.example")  # Already pending.
        invitation = services.pending_invitations_for(invited).get()
        with self.assertRaises(services.NotFound):  # Someone else cannot accept it.
            services.accept_invitation(self.member, invitation.pk)
        services.accept_invitation(invited, invitation.pk)
        self.assertEqual(services.membership_for(invited, self.org).role, "MANAGER")
        with self.assertRaises(services.InvalidState):
            services.accept_invitation(invited, invitation.pk)
        with self.assertRaises(ValidationError):
            services.invite_member(self.owner, self.org, "teammate@brand.example")  # Already a member.
        event = AuditEvent.objects.get(action="advertiser.member.invited")
        self.assertNotIn("teammate", str(event.metadata) + event.target_repr)  # Invitee address not audited.

    def test_expired_and_staff_invitations(self):
        services.invite_member(self.owner, self.org, self.manager.email)
        invitation = AdvertiserInvitation.objects.get(email=self.manager.email)
        with self.assertRaises(services.NotAuthorized):
            services.accept_invitation(self.manager, invitation.pk)  # Staff never join (no self-approval).
        late = make_user("late@x.example")
        services.invite_member(self.owner, self.org, late.email)
        AdvertiserInvitation.objects.filter(email=late.email).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertFalse(services.pending_invitations_for(late).exists())
        with self.assertRaises(services.InvalidState):
            services.accept_invitation(late, AdvertiserInvitation.objects.get(email=late.email).pk)

    def test_members_only_invite_if_owner_and_last_owner_stays(self):
        manager = make_user("mgr@brand.example")
        services.invite_member(self.owner, self.org, manager.email)
        services.accept_invitation(manager, services.pending_invitations_for(manager).get().pk)
        with self.assertRaises(services.NotAuthorized):
            services.invite_member(manager, self.org, "x@y.example")
        owner_membership = services.membership_for(self.owner, self.org)
        with self.assertRaises(services.NotAuthorized):
            services.remove_member(manager, self.org, owner_membership.pk)
        with self.assertRaises(services.InvalidState):
            services.remove_member(self.owner, self.org, owner_membership.pk)
        services.remove_member(manager, self.org, services.membership_for(manager, self.org).pk)  # Leave.
        self.assertIsNone(services.membership_for(manager, self.org))
        self.assertFalse(services.organizations_for(manager).exists())

    def test_inactive_account_loses_access(self):
        self.assertTrue(services.organizations_for(self.owner).exists())
        self.owner.status = User.Status.SUSPENDED
        self.owner.save()
        self.assertFalse(services.organizations_for(self.owner).exists())
        with self.assertRaises(services.NotFound):
            services.organization_for(self.owner, self.org.pk)
