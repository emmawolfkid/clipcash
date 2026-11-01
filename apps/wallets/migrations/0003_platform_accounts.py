"""
Create the platform-scoped ledger accounts (chart of accounts).

Accounts only: no journal entries, balances or deposits are created. Scoped
accounts (per advertiser, campaign or member) are opened by later modules.
The definitions are frozen here so later chart edits do not change history.
"""

from django.db import migrations

PLATFORM_ACCOUNTS = [
    # (type, name, classification, funding_class, overdraft_protected)
    ("CASH", "Cash at bank and mobile-money float", "ASSET", "TREASURY", False),
    ("PAYMENT_CLEARING", "Payment provider clearing", "ASSET", "TREASURY", False),
    ("MEMBERSHIP_DEFERRED_REVENUE", "Membership fees received in advance", "LIABILITY", "MEMBERSHIP", True),
    ("WITHDRAWAL_CLEARING", "Withdrawals in progress", "LIABILITY", "REWARD_RESTRICTED", True),
    ("REFUNDS_PAYABLE", "Refunds payable", "LIABILITY", "OPERATING", True),
    ("OWNER_EQUITY", "Owner equity", "EQUITY", "OPERATING", False),
    ("MEMBERSHIP_REVENUE", "Membership revenue", "REVENUE", "MEMBERSHIP", False),
    ("ADVERTISING_SERVICE_REVENUE", "Advertising service revenue", "REVENUE", "OPERATING", False),
    ("OTHER_OPERATING_REVENUE", "Other operating revenue", "REVENUE", "OPERATING", False),
    ("PAYMENT_PROCESSING_FEES", "Payment processing fees", "EXPENSE", "OPERATING", False),
    ("OPERATING_EXPENSES", "Operating expenses", "EXPENSE", "OPERATING", False),
]


def create_platform_accounts(apps, schema_editor):
    LedgerAccount = apps.get_model("wallets", "LedgerAccount")
    for account_type, name, classification, funding_class, protected in PLATFORM_ACCOUNTS:
        LedgerAccount.objects.get_or_create(
            account_type=account_type,
            scope_type="PLATFORM",
            scope_id="",
            defaults={
                "code": account_type.lower(),
                "name": name,
                "classification": classification,
                "funding_class": funding_class,
                "overdraft_protected": protected,
                "currency": "TZS",
                "status": "ACTIVE",
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ("wallets", "0002_ledger_integrity"),
    ]

    operations = [
        # Reverse is a no-op: accounts are never deleted.
        migrations.RunPython(create_platform_accounts, migrations.RunPython.noop),
    ]
