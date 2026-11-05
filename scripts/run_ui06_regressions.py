"""UI-06 advertiser workspace coverage plus accepted frontend regressions."""
import run_ui05_regressions  # Extends the guarded regression labels.
import run_ui02_regressions as guarded

WHOLE_PACKAGES = ("apps.administration.", "apps.advertising.", "apps.reports.")

guarded.TEST_LABELS = tuple(label for label in guarded.TEST_LABELS if not label.startswith(WHOLE_PACKAGES)) + (
    "apps.administration.tests", "apps.advertising.tests", "apps.reports.tests",
)

if __name__ == "__main__":
    guarded.main(suite_name="UI-06")
