"""UI-04 member/video/viewing coverage plus accepted frontend regressions."""
import run_ui03_regressions  # Extends the guarded regression labels.
import run_ui02_regressions as guarded

guarded.TEST_LABELS = tuple(label for label in guarded.TEST_LABELS
                          if not label.startswith(("apps.viewing.", "apps.videos."))) + (
    "apps.core.tests.test_member_viewing_ui", "apps.viewing.tests", "apps.videos.tests",
)

if __name__ == "__main__":
    guarded.main(suite_name="UI-04")
