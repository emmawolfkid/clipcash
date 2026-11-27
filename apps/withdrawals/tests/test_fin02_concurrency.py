"""FIN-02 concurrency on the real database (MariaDB here): real threads and row locks. The same
money is never reserved twice, a submission is created once, a payout is claimed by one operator
and paid once."""

import secrets

from django.test import TransactionTestCase, override_settings

from apps.earnings import services as earnings
from apps.earnings.tests.test_concurrency import EarningsConcurrencyTests
from apps.earnings.tests.helpers import ACTIVE, staff
from apps.memberships.tests.test_concurrency import run_threads
from apps.videos.tests.helpers import BUNNY
from apps.wallets import wallet_services
from apps.wallets.models import JournalEntry, Wallet
from apps.withdrawals import real, services
from apps.withdrawals.models import WithdrawalRequest

from .fin02_helpers import WITHDRAWALS_ON


@BUNNY
@ACTIVE
@WITHDRAWALS_ON
@override_settings(VIEWING_MAX_EVENTS_PER_MINUTE=1000, VIEWING_MAX_STARTS_PER_HOUR=1000)
class WithdrawalConcurrencyTests(TransactionTestCase):
    setUp_earnings = EarningsConcurrencyTests.setUp
    member = EarningsConcurrencyTests.member
    deposit = EarningsConcurrencyTests.deposit
    verified_reward = EarningsConcurrencyTests.verified_reward

    def setUp(self):
        self.setUp_earnings()
        self.deposit(1_000)
        from apps.earnings import funding

        funding.commit_funding(self.recorder, self.campaign, amount=1_000)
        reward = self.verified_reward()  # Member m99 earns TZS 100.
        earnings.settle(reward.pk)
        self.clock.advance(25 * 3600)
        earnings.clear_due()
        self.payee = reward.member
        settings_admin = staff("wd-settings@x.example", "withdrawals.manage_withdrawal_settings")
        current = real.current_settings()
        values = {name: getattr(current, name) for name in real.SETTING_FIELDS}
        values.update(min_amount=10, daily_amount_limit="", daily_request_limit="", identity_required_from="")
        real.update_settings(settings_admin, expected_version=current.version, reason="Concurrency test", **values)
        self.beneficiary = services.add_beneficiary(self.payee, kind="MOBILE_MONEY", network="MPESA",
                                                    identifier="0754123456", holder_name="Racer")
        self.clock.advance(25 * 3600)
        self.approver = staff("wd-approver@x.example", "withdrawals.approve_withdrawals")
        self.operators = [staff(f"op{i}@x.example", "withdrawals.process_payouts") for i in range(2)]
        self.verifiers = [staff(f"pv{i}@x.example", "withdrawals.verify_payouts") for i in range(2)]

    def balances(self):
        return wallet_services.balances(Wallet.objects.get(member=self.payee))

    def submit(self, amount, tok=None):
        return real.submit(self.payee, beneficiary_id=self.beneficiary.pk, amount=amount,
                           token=tok or secrets.token_urlsafe(24))

    def test_racing_requests_never_reserve_the_same_money(self):
        self.assertEqual(self.balances().available, 100)
        results = run_threads(4, lambda i: self.submit(60))
        created = [r for r in results if isinstance(r, WithdrawalRequest)]
        self.assertEqual(len(created), 1, results)
        self.assertEqual((self.balances().available, self.balances().reserved), (40, 60))

    def test_the_same_submission_creates_one_request(self):
        tok = secrets.token_urlsafe(24)
        results = run_threads(5, lambda i: self.submit(30, tok))
        self.assertEqual(len({r.pk for r in results if isinstance(r, WithdrawalRequest)}), 1, results)
        self.assertEqual(WithdrawalRequest.objects.count(), 1)
        self.assertEqual(self.balances().reserved, 30)

    def test_one_operator_claims_and_the_payout_is_paid_once(self):
        withdrawal = self.submit(80)
        real.approve(self.approver, withdrawal, expected_status="REQUESTED")
        results = run_threads(2, lambda i: real.claim(self.operators[i], withdrawal))
        self.assertEqual(sum(1 for r in results if isinstance(r, WithdrawalRequest)), 1, results)
        withdrawal.refresh_from_db()
        operator = withdrawal.claimed_by
        real.record_payment(operator, withdrawal, payout_reference="MPRACE0001", evidence="Sent once")
        run_threads(2, lambda i: real.verify_payment(self.verifiers[i], withdrawal, confirmed=True))
        self.assertEqual(JournalEntry.objects.filter(posting_type="withdrawal.paid_manual").count(), 1)
        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.status, "PAID")
        self.assertEqual((self.balances().available, self.balances().reserved), (20, 0))
        self.assertEqual(real.reconcile(), [])


del EarningsConcurrencyTests  # Imported only for its fixture; its own tests run in their module.
