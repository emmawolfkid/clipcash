"""
Chart of accounts (finance-reviewable definitions).

Accounting classification (asset, liability, ...) is separate from the
*funding class*, which controls which money may finance rewards. Neither the
account names nor these classes establish legal escrow or trust status; that
depends on contracts, bank arrangements and regulatory approval (see
docs/architecture/ledger.md, "Assumptions requiring review").

This module is plain data: migrations, models and the database triggers all
derive from it, so the definitions exist in exactly one place.
"""

from dataclasses import dataclass


class Classification:
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    REVENUE = "REVENUE"
    EXPENSE = "EXPENSE"

    DEBIT_NORMAL = (ASSET, EXPENSE)
    CREDIT_NORMAL = (LIABILITY, EQUITY, REVENUE)
    choices = [(c, c.title()) for c in (ASSET, LIABILITY, EQUITY, REVENUE, EXPENSE)]


class FundingClass:
    TREASURY = "TREASURY"                      # Cash and provider clearing
    MEMBERSHIP = "MEMBERSHIP"                  # Membership fees; never reward funding
    ADVERTISER_UNVERIFIED = "ADVERTISER_UNVERIFIED"
    ADVERTISER_VERIFIED = "ADVERTISER_VERIFIED"
    REWARD_RESTRICTED = "REWARD_RESTRICTED"    # Committed campaign funds and reward obligations
    OPERATING = "OPERATING"                    # Platform revenue, expenses, equity, refunds

    choices = [(c, c.replace("_", " ").title()) for c in (
        TREASURY, MEMBERSHIP, ADVERTISER_UNVERIFIED, ADVERTISER_VERIFIED, REWARD_RESTRICTED, OPERATING,
    )]


class Scope:
    PLATFORM = "PLATFORM"
    ADVERTISER = "ADVERTISER"
    CAMPAIGN = "CAMPAIGN"
    MEMBER = "MEMBER"

    choices = [(c, c.title()) for c in (PLATFORM, ADVERTISER, CAMPAIGN, MEMBER)]


@dataclass(frozen=True)
class AccountType:
    code: str
    number: str             # Chart number for finance review
    name: str
    classification: str
    funding_class: str
    scope: str
    overdraft_protected: bool  # Normal balance may never go below zero
    description: str


ACCOUNT_TYPES = {
    t.code: t
    for t in (
        # Assets
        AccountType("CASH", "1000", "Cash at bank and mobile-money float", Classification.ASSET,
                    FundingClass.TREASURY, Scope.PLATFORM, False,
                    "Settled funds held by ClipCash."),
        AccountType("PAYMENT_CLEARING", "1100", "Payment provider clearing", Classification.ASSET,
                    FundingClass.TREASURY, Scope.PLATFORM, False,
                    "Funds confirmed by a payment provider but not yet settled to ClipCash."),
        AccountType("ADVERTISER_RECEIVABLE", "1200", "Advertiser receivables", Classification.ASSET,
                    FundingClass.ADVERTISER_UNVERIFIED, Scope.ADVERTISER, False,
                    "Amounts invoiced to an advertiser and not yet received. Never reward funding."),
        # Liabilities
        AccountType("MEMBERSHIP_DEFERRED_REVENUE", "2000", "Membership fees received in advance",
                    Classification.LIABILITY, FundingClass.MEMBERSHIP, Scope.PLATFORM, True,
                    "Fees for membership periods not yet delivered."),
        AccountType("ADVERTISER_FUNDING_PENDING", "2100", "Advertiser funds received, pending verification",
                    Classification.LIABILITY, FundingClass.ADVERTISER_UNVERIFIED, Scope.ADVERTISER, True,
                    "Received but not yet independently verified; cannot fund campaigns."),
        AccountType("ADVERTISER_FUNDING_VERIFIED", "2110", "Advertiser funds verified, unallocated",
                    Classification.LIABILITY, FundingClass.ADVERTISER_VERIFIED, Scope.ADVERTISER, True,
                    "Verified advertiser money not yet committed to a campaign."),
        AccountType("CAMPAIGN_RESTRICTED_FUNDING", "2200", "Campaign restricted reward funding",
                    Classification.LIABILITY, FundingClass.REWARD_RESTRICTED, Scope.CAMPAIGN, True,
                    "Verified advertiser money committed to one campaign; the only source of rewards."),
        AccountType("REWARD_LIABILITY", "2300", "Rewards owed to members",
                    Classification.LIABILITY, FundingClass.REWARD_RESTRICTED, Scope.MEMBER, True,
                    "Rewards allocated from campaign funding; not withdrawable in this phase."),
        # Member wallet buckets (Phase 09). Same funding class as REWARD_LIABILITY, so the
        # database funding-separation rules apply unchanged. Postings to wallet-linked
        # accounts are blocked by the database until a reviewed activation migration.
        AccountType("MEMBER_REWARD_PENDING", "2310", "Member rewards pending clearance",
                    Classification.LIABILITY, FundingClass.REWARD_RESTRICTED, Scope.MEMBER, True,
                    "Rewards awaiting a clearance window; not spendable."),
        AccountType("MEMBER_WITHDRAWAL_RESERVED", "2320", "Member withdrawal reservations",
                    Classification.LIABILITY, FundingClass.REWARD_RESTRICTED, Scope.MEMBER, True,
                    "Amounts reserved for a requested withdrawal; not spendable."),
        AccountType("WITHDRAWAL_CLEARING", "2400", "Withdrawals in progress",
                    Classification.LIABILITY, FundingClass.REWARD_RESTRICTED, Scope.PLATFORM, True,
                    "Approved withdrawals awaiting provider payout."),
        AccountType("REFUNDS_PAYABLE", "2500", "Refunds payable",
                    Classification.LIABILITY, FundingClass.OPERATING, Scope.PLATFORM, True,
                    "Approved refunds not yet paid out."),
        # Equity
        AccountType("OWNER_EQUITY", "3000", "Owner equity", Classification.EQUITY,
                    FundingClass.OPERATING, Scope.PLATFORM, False, "Capital and retained earnings."),
        # Revenue
        AccountType("MEMBERSHIP_REVENUE", "4000", "Membership revenue", Classification.REVENUE,
                    FundingClass.MEMBERSHIP, Scope.PLATFORM, False,
                    "Earned membership fees. Platform income; never reward funding."),
        AccountType("ADVERTISING_SERVICE_REVENUE", "4100", "Advertising service revenue",
                    Classification.REVENUE, FundingClass.OPERATING, Scope.PLATFORM, False,
                    "Platform fees earned on advertising (requires finance definition)."),
        AccountType("OTHER_OPERATING_REVENUE", "4900", "Other operating revenue", Classification.REVENUE,
                    FundingClass.OPERATING, Scope.PLATFORM, False, "Other platform income."),
        # Expenses
        AccountType("PAYMENT_PROCESSING_FEES", "5000", "Payment processing fees", Classification.EXPENSE,
                    FundingClass.OPERATING, Scope.PLATFORM, False, "Fees charged by payment providers."),
        AccountType("OPERATING_EXPENSES", "5900", "Operating expenses", Classification.EXPENSE,
                    FundingClass.OPERATING, Scope.PLATFORM, False, "Other platform costs."),
    )
}

PLATFORM_ACCOUNT_TYPES = [t for t in ACCOUNT_TYPES.values() if t.scope == Scope.PLATFORM]


def account_code(type_code, scope_id=""):
    """Stable identifier: ``campaign_restricted_funding:<campaign-id>`` or ``cash``."""
    base = type_code.lower()
    return f"{base}:{scope_id}" if scope_id else base


# --- Database-enforced funding rules (see migration 0002) -------------------------
#
# For an entry that CREDITS an account in a key funding class, every DEBIT line
# must come from one of the listed funding classes. Combined with the rule that
# membership money never shares an entry with advertiser or reward money, this
# makes "rewards funded by membership fees" impossible at the database level.
PROTECTED_INFLOWS = {
    FundingClass.REWARD_RESTRICTED: (FundingClass.ADVERTISER_VERIFIED, FundingClass.REWARD_RESTRICTED),
    FundingClass.ADVERTISER_VERIFIED: (FundingClass.ADVERTISER_UNVERIFIED, FundingClass.REWARD_RESTRICTED),
}
SEGREGATED_FROM_MEMBERSHIP = (
    FundingClass.ADVERTISER_UNVERIFIED,
    FundingClass.ADVERTISER_VERIFIED,
    FundingClass.REWARD_RESTRICTED,
)
