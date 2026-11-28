"""REF-01 test helpers: an isolated configuration with referral rewards, real rewards and withdrawals
switched on (test settings and a test-only encryption key; nothing outside the test database)."""

import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone

from apps.accounts import services as accounts
from apps.accounts.tests.helpers import PASSWORD
from apps.earnings.tests.helpers import ACTIVE, reference, staff
from apps.referrals import funding, services
from apps.withdrawals.tests.fin02_helpers import WITHDRAWALS_ON, PayoutTestCase

User = get_user_model()
REFERRALS_ON = override_settings(REFERRAL_REWARDS_ENABLED=True, REFERRAL_REWARDS_APPROVED=True)
REFERRALS_OFF = override_settings(REFERRAL_REWARDS_ENABLED=False, REFERRAL_REWARDS_APPROVED=False)
RULES = {"tracking_open": True, "rewards_open": True, "reward_amount": 1000, "referrer_requires_membership": True,
         "referred_requires_membership": False, "min_account_age_days": 1, "required_verified_views": 2,
         "required_view_days": 2, "qualification_days": 30, "daily_reward_limit": 5, "lifetime_reward_limit": 10,
         "clearance_hours": 24}


@ACTIVE
@WITHDRAWALS_ON
@REFERRALS_ON
class ReferralTestCase(PayoutTestCase):
    """PayoutTestCase (``member`` with a membership, FIN-01/FIN-02 staff) plus referral staff:
    ``proposer`` proposes rules, ``policy_approver`` approves them, ``reviewer`` reviews referrals,
    ``mk_recorder`` records and commits the marketing budget, ``mk_verifier`` verifies deposits."""

    def setUp(self):
        super().setUp()
        self.proposer = staff("ref-proposer@x.example", "referrals.manage_referral_policy")
        self.policy_approver = staff("ref-approver@x.example", "referrals.approve_referral_policy")
        self.reviewer = staff("ref-reviewer@x.example", "referrals.review_referrals", "referrals.view_referrals")
        self.mk_recorder = staff("mk-recorder@x.example", "referrals.record_marketing_deposits",
                                 "referrals.allocate_referral_budget")
        self.mk_verifier = staff("mk-verifier@x.example", "referrals.verify_marketing_deposits")
        self.code = services.ensure_code(self.member)
        self.qcampaign = self.running_campaign(name="Qualifying views", max_views_per_member=10,
                                               ends_at=timezone.now() + timedelta(days=120))

    def rules(self, **changes):
        """Propose and approve a new policy version (four-eyes)."""
        current = services.current_policy()
        values = {name: getattr(current, name) for name in services.POLICY_FIELDS}
        values.update(RULES if current.version == 1 else {})
        values.update(changes)
        values = {key: ("" if value is None else value) for key, value in values.items()}
        proposal = services.propose_policy(self.proposer, expected_version=current.version, reason="Test rules",
                                           **values)
        return services.decide_policy(self.policy_approver, proposal, approve=True, note="Approved in test")

    def fund(self, amount=10_000, commit=None):
        deposit = funding.record_deposit(self.mk_recorder, method="AIRTEL_LIPA", amount=amount,
                                         external_reference=reference(), paid_on=timezone.localdate())
        funding.verify_deposit(self.mk_verifier, deposit, receipt_confirmed=True)
        return funding.commit_budget(self.mk_recorder, amount=commit or amount)

    def join(self, code=None, email=None, source="LINK", membership=False, first="Neema", last="Mushi"):
        """Register a new member with the referral code (the real registration service)."""
        email = email or f"new-{secrets.token_hex(4)}@x.example"
        code = self.code.code if code is None else code
        user = accounts.register_member(contact_method="email", contact=email, password=PASSWORD, first_name=first,
                                        last_name=last, referral_code=code, referral_source=source)
        User.objects.filter(pk=user.pk).update(email_verified=True)
        user = User.objects.get(pk=user.pk)
        if membership:
            self.active_membership(member=user)
        return user

    def qualify(self, user, views=2):
        """Genuine activity: verified sponsored views on different days (membership needed to watch)."""
        from apps.memberships.access import check_access

        if not check_access(user):
            self.active_membership(member=user)
        for _index in range(views):
            self.verified_view(member=user, campaign=self.qcampaign)
            self.clock.advance(24 * 3600 + 60)
        return user.referral

    def referral_for(self, user):
        user.referral.refresh_from_db()
        return user.referral


__all__ = ["REFERRALS_OFF", "REFERRALS_ON", "RULES", "ReferralTestCase", "User", "reference", "staff"]
