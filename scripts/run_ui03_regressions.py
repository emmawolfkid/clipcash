"""UI-03 public/authentication coverage plus the existing UI-02 regression suite."""
import run_ui02_regressions as guarded

guarded.TEST_LABELS = tuple(label for label in guarded.TEST_LABELS if not label.startswith("apps.accounts.")) + (
    "apps.core.tests.test_public_auth_ui",
    "apps.accounts.tests",
)

if __name__ == "__main__":
    guarded.main(suite_name="UI-03")
