from django.apps import AppConfig


class ViewingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.viewing"

    def ready(self):
        from . import checks, jobs  # noqa: F401  (system checks, job types and schedules)
