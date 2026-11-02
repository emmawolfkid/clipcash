from apps.rewards import services
from apps.rewards.models import DemoReward, RewardPolicy
from apps.viewing.tests.helpers import User, ViewingTestCase, WatchSession, make_user


class RewardsTestCase(ViewingTestCase):
    """Adds a second Super Administrator (four-eyes approvals) and helpers for verified views."""

    def setUp(self):
        super().setUp()
        self.approver = make_user("approver@example.com", role=User.Role.SUPER_ADMIN)

    def approved_policy(self, campaign=None, amount=100):
        policy = services.create_policy(self.root, campaign or self.campaign, reward_amount=amount)
        return services.approve_policy(self.approver, policy)

    def verified_session(self, member=None, campaign=None):
        member = member or self.member
        session = self.watch(self.start(member=member, campaign=campaign), 95) if member == self.member else None
        if session is None:
            session = self.start(member=member, campaign=campaign)
            seq = 1
            self.event(session, seq, 0, member=member)
            for position in range(10, 100, 10):
                seq += 1
                self.event(session, seq, min(position, 95), member=member, after=10)
            self.event(session, seq + 1, 95, state="ended", member=member, after=1)
            session.refresh_from_db()
        assert session.outcome == WatchSession.Outcome.VERIFIED_EVIDENCE, session.reasons
        return session

    def new_member(self, email):
        member = make_user(email)
        self.active_membership(member=member)
        return member


__all__ = ["DemoReward", "RewardPolicy", "RewardsTestCase", "User", "WatchSession", "make_user"]
