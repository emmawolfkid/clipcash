"""UI-05 financial experience coverage plus accepted frontend regressions."""
import run_ui04_regressions  # Extends the guarded regression labels.
import run_ui02_regressions as guarded

WHOLE_PACKAGES = ("apps.accounts.", "apps.memberships.", "apps.payments.", "apps.rewards.", "apps.wallets.",
                  "apps.withdrawals.")

guarded.TEST_LABELS = tuple(label for label in guarded.TEST_LABELS if not label.startswith(WHOLE_PACKAGES)) + (
    "apps.core.tests.test_financial_ui", "apps.accounts.tests", "apps.memberships.tests", "apps.payments.tests",
    "apps.rewards.tests", "apps.wallets.tests", "apps.withdrawals.tests",
)

if __name__ == "__main__":
    guarded.main(suite_name="UI-05")
