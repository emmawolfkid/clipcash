"""UI-07 administration console coverage plus accepted frontend regressions."""
import run_ui06_regressions  # Extends the guarded regression labels.
import run_ui02_regressions as guarded

WHOLE_PACKAGES = ("apps.audit.", "apps.notifications.", "apps.support.")

guarded.TEST_LABELS = tuple(label for label in guarded.TEST_LABELS if not label.startswith(WHOLE_PACKAGES)) + (
    "apps.audit.tests", "apps.notifications.tests", "apps.support.tests",
)

if __name__ == "__main__":
    guarded.main(suite_name="UI-07")
