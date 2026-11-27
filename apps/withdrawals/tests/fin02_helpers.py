"""FIN-02 test helpers: an isolated configuration with real rewards AND real withdrawals switched on,
a test-only encryption key, and separated finance staff. Nothing leaves the test database."""

import secrets
from cryptography.fernet import Fernet
from django.test import override_settings

from apps.earnings import services as earnings
from apps.earnings.tests.helpers import ACTIVE, FinanceTestCase, staff
from apps.wallets import wallet_services
from apps.wallets.models import Wallet
from apps.withdrawals import real, services
from apps.withdrawals.models import PayoutBeneficiary

TEST_KEY = Fernet.generate_key().decode()
WITHDRAWALS_ON = override_settings(WITHDRAWALS_ENABLED=True, WITHDRAWALS_APPROVED=True, PAYOUT_DATA_KEYS=[TEST_KEY])


def token():
    return secrets.token_urlsafe(24)


@ACTIVE
@WITHDRAWALS_ON
class PayoutTestCase(FinanceTestCase):
    """FinanceTestCase plus payout staff: ``approver2`` approves withdrawals, ``operator`` sends payouts,
    ``payout_verifier`` verifies them, ``settings_admin`` changes settings, ``id_checker`` checks identity."""

    def setUp(self):
        super().setUp()
        self.approver2 = staff("wd-approver@x.example", "withdrawals.approve_withdrawals")
        self.operator = staff("operator@x.example", "withdrawals.process_payouts")
        self.payout_verifier = staff("payout-verifier@x.example", "withdrawals.verify_payouts")
        self.settings_admin = staff("wd-settings@x.example", "withdrawals.manage_withdrawal_settings")
        self.id_checker = staff("id-checker@x.example", "withdrawals.verify_withdrawal_identity")
        self.configure()

    def configure(self, **values):
        current = real.current_settings()
        base = {name: getattr(current, name) for name in real.SETTING_FIELDS}
        base.update({"min_amount": 50, "max_amount": 100_000, "identity_required_from": None,
                     "daily_amount_limit": None, "daily_request_limit": None})
        base.update(values)
        for name in ("daily_amount_limit", "daily_request_limit", "identity_required_from"):
            base[name] = "" if base[name] is None else base[name]
        return real.update_settings(self.settings_admin, expected_version=current.version, reason="Test settings",
                                    **base)

    def earn(self, amount=500, member=None):
        """A real, cleared reward: funded campaign, verified view, clearance (FIN-01 path)."""
        member = member or self.member
        campaign = self.running_campaign(name=f"Earn {secrets.token_hex(3)}", demo_reward_per_view=max(amount, 100))
        self.fund(campaign=campaign, amount=amount * 4)
        self.tzs_policy(campaign=campaign, amount=amount)
        session = self.verified_view(member=member, campaign=campaign)
        self.clock.advance(25 * 3600)
        earnings.clear_due()
        return session.earning

    def destination(self, member=None, network="MPESA", number="0754123456", holder="Asha Juma", age_hours=25):
        member = member or self.member
        if network == "BANK":
            beneficiary = services.add_beneficiary(member, kind="BANK", bank_code="CRDB", identifier=number,
                                                   holder_name=holder)
        else:
            beneficiary = services.add_beneficiary(member, kind="MOBILE_MONEY", network=network, identifier=number,
                                                   holder_name=holder)
        if age_hours:
            # Destinations are immutable (database guard), so time moves on instead: past the cooling-off.
            self.clock.advance(age_hours * 3600)
        return beneficiary

    def balances(self, member=None):
        return wallet_services.balances(Wallet.objects.get(member=member or self.member))

    def submit(self, amount=200, beneficiary=None, member=None, tok=None):
        return real.submit(member or self.member, beneficiary_id=(beneficiary or self.beneficiary).pk, amount=amount,
                           token=tok or token())

    def pay(self, withdrawal, reference=None):
        """Approve -> claim -> record -> verify, by four different people."""
        real.approve(self.approver2, withdrawal, expected_status=withdrawal.status)
        real.claim(self.operator, withdrawal)
        real.record_payment(self.operator, withdrawal, payout_reference=reference or f"MP{secrets.token_hex(5).upper()}",
                            evidence="Sent from the ClipCash M-Pesa till")
        return real.verify_payment(self.payout_verifier, withdrawal, confirmed=True)


__all__ = ["PayoutTestCase", "WITHDRAWALS_ON", "TEST_KEY", "token", "staff"]
