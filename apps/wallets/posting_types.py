"""
Controlled posting types: the only kinds of journal entry the service accepts.

Each type fixes which account types may be debited and credited, which
financial feature must be enabled, and extra rules. Every type here requires a
financial feature that is locked off in code (apps.core.features), except
membership.manual_payment_received (PAY-01), which needs the environment-owned
manual_membership_payments feature and is posted only after staff verification. The database independently enforces
balance, overdraft and funding-separation rules (migration 0002).
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PostingType:
    code: str
    description: str
    debit_types: frozenset
    credit_types: frozenset
    required_feature: str
    requires_external_reference: bool = False
    # Posting type the ``related_entry`` must have (and amount it must equal).
    related_type: str | None = None
    related_amount_must_match: bool = False
    # The posting actor must be a person different from the related entry's actor.
    distinct_actor: bool = False
    # All lines must share the same scope id (e.g. one advertiser).
    same_scope: bool = False
    extra: dict = field(default_factory=dict)


def _type(code, description, debit, credit, feature, **options):
    return PostingType(code, description, frozenset(debit), frozenset(credit), feature, **options)


POSTING_TYPES = {
    t.code: t
    for t in (
        # Membership (platform income; never reward funding)
        _type("membership.payment_received", "Membership fee received",
              ["PAYMENT_CLEARING"], ["MEMBERSHIP_DEFERRED_REVENUE"], "live_payments",
              requires_external_reference=True),
        _type("membership.revenue_recognized", "Membership fee earned",
              ["MEMBERSHIP_DEFERRED_REVENUE"], ["MEMBERSHIP_REVENUE"], "live_payments"),
        _type("membership.refund_approved", "Membership refund approved",
              ["MEMBERSHIP_DEFERRED_REVENUE"], ["REFUNDS_PAYABLE"], "live_payments"),
        # PAY-01: a membership fee paid to ClipCash's Airtel Lipa business number and
        # verified by staff in the official Airtel records. The money is already in the
        # mobile-money float (CASH), so there is no provider clearing step. Posted only by
        # apps.payments.manual, once per Airtel transaction (external reference).
        _type("membership.manual_payment_received", "Membership fee received (Airtel Lipa, staff-verified)",
              ["CASH"], ["MEMBERSHIP_DEFERRED_REVENUE"], "manual_membership_payments",
              requires_external_reference=True),
        # Advertiser funding
        _type("advertiser.funding_received", "Advertiser payment received (unverified)",
              ["PAYMENT_CLEARING"], ["ADVERTISER_FUNDING_PENDING"], "live_payments",
              requires_external_reference=True),
        _type("advertiser.funding_verified", "Advertiser payment independently verified",
              ["ADVERTISER_FUNDING_PENDING"], ["ADVERTISER_FUNDING_VERIFIED"], "live_payments",
              related_type="advertiser.funding_received", related_amount_must_match=True,
              distinct_actor=True, same_scope=True),
        _type("advertiser.refund_approved", "Unallocated advertiser funds to be refunded",
              ["ADVERTISER_FUNDING_VERIFIED"], ["REFUNDS_PAYABLE"], "live_payments"),
        # Campaign funding and rewards
        _type("campaign.funding_committed", "Verified funds committed to a campaign",
              ["ADVERTISER_FUNDING_VERIFIED"], ["CAMPAIGN_RESTRICTED_FUNDING"], "real_money_rewards"),
        _type("campaign.funding_released", "Unused campaign funds released",
              ["CAMPAIGN_RESTRICTED_FUNDING"], ["ADVERTISER_FUNDING_VERIFIED"], "real_money_rewards"),
        _type("reward.allocated", "Reward allocated from campaign funding",
              ["CAMPAIGN_RESTRICTED_FUNDING"], ["REWARD_LIABILITY"], "real_money_rewards"),
        # Withdrawals
        _type("withdrawal.approved", "Withdrawal approved",
              ["REWARD_LIABILITY"], ["WITHDRAWAL_CLEARING"], "withdrawals"),
        _type("withdrawal.paid", "Withdrawal paid out by provider",
              ["WITHDRAWAL_CLEARING"], ["PAYMENT_CLEARING"], "withdrawals",
              requires_external_reference=True, related_type="withdrawal.approved",
              related_amount_must_match=True),
        # Treasury
        _type("payment.settled", "Provider settlement to bank",
              ["CASH"], ["PAYMENT_CLEARING"], "live_payments", requires_external_reference=True),
        _type("payment.fee_charged", "Provider fee charged",
              ["PAYMENT_PROCESSING_FEES"], ["PAYMENT_CLEARING"], "live_payments"),
        _type("refund.paid", "Refund paid out by provider",
              ["REFUNDS_PAYABLE"], ["PAYMENT_CLEARING"], "live_payments", requires_external_reference=True),
    )
}

# Posting types whose related entry may be used only once (no double
# verification, no double payout). Also enforced by a partial unique index.
SINGLE_USE_RELATED_TYPES = ("advertiser.funding_verified", "withdrawal.paid")
