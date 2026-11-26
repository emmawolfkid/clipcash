"""FIN-01 test helpers. ``ACTIVE`` is an isolated test configuration that switches real-money rewards
on (both environment settings, no restricted demo, no sandbox payments); nothing outside the test
database is touched and no real deposit is involved."""

import uuid

from django.contrib.auth.models import Permission
from django.test import override_settings
from django.utils import timezone

from apps.accounts.tests.helpers import User, make_user
from apps.earnings import funding
from apps.rewards import services as rewards
from apps.viewing.tests.helpers import ViewingTestCase

ACTIVE = override_settings(REAL_MONEY_REWARDS_ENABLED=True, REAL_MONEY_REWARDS_APPROVED=True,
                           RESTRICTED_DEMO_MODE=False, PAYMENTS_MODE="disabled")


def staff(email, *perms):
    user = make_user(email, role=User.Role.CONTENT_MANAGER)
    for dotted in perms:
        app_label, codename = dotted.split(".")
        user.user_permissions.add(Permission.objects.get(content_type__app_label=app_label, codename=codename))
    return User.objects.get(pk=user.pk)  # Fresh instance: no stale permission cache.


def reference():
    return "MP" + uuid.uuid4().hex[:10].upper()


@ACTIVE
class FinanceTestCase(ViewingTestCase):
    """ViewingTestCase (``member`` with a plan, running ``campaign`` with a 95 s video, ``org``)
    plus separated finance staff: ``recorder`` records deposits and allocates funding, ``verifier``
    verifies receipts, ``approver`` approves reward policies, ``rewards_admin`` manages rewards."""

    def setUp(self):
        super().setUp()
        self.recorder = staff("recorder@x.example", "earnings.record_deposits", "earnings.allocate_funding",
                              "earnings.view_earnings")
        self.verifier = staff("verifier@x.example", "earnings.verify_deposits")
        self.approver = staff("approver@x.example", "rewards.approve_reward_policies")
        self.rewards_admin = staff("rewards@x.example", "earnings.manage_real_rewards", "earnings.manage_earning_rules",
                                   "viewing.review_viewing")

    def deposit(self, org=None, amount=10_000, method="AIRTEL_LIPA", ref=None, verify=True):
        deposit = funding.record_deposit(self.recorder, organization=org or self.org, method=method, amount=amount,
                                         external_reference=ref or reference(), paid_on=timezone.localdate())
        return funding.verify_deposit(self.verifier, deposit, receipt_confirmed=True) if verify else deposit

    def fund(self, campaign=None, amount=1_000, org=None):
        campaign = campaign or self.campaign
        self.deposit(org=org or campaign.organization, amount=amount)
        return funding.commit_funding(self.recorder, campaign, amount=amount)

    def tzs_policy(self, campaign=None, amount=100):
        policy = rewards.create_policy(self.root, campaign or self.campaign, reward_amount=amount, kind="TZS")
        return rewards.approve_policy(self.approver, policy)

    def new_member(self, email, plan=None):
        member = make_user(email)
        self.active_membership(member=member, plan=plan)
        return member

    def play(self, session, member=None, until_s=95, end=True):
        """Honest playback by ``member``; closing callbacks (settlement) run on commit."""
        member = member or session.member
        with self.captureOnCommitCallbacks(execute=True):
            self.event(session, 1, 0, member=member)
            seq, position = 1, 0
            while position < until_s:
                seq, position = seq + 1, min(until_s, position + 10)
                self.event(session, seq, position, member=member, after=10)
            if end:
                self.event(session, seq + 1, until_s, state="ended", member=member, after=1)
        session.refresh_from_db()
        return session

    def verified_view(self, member=None, campaign=None):
        member = member or self.member
        session = self.start(member=member, campaign=campaign)
        return self.play(session, member=member)


__all__ = ["ACTIVE", "FinanceTestCase", "make_user", "reference", "staff"]
