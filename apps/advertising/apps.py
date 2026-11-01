from django.apps import AppConfig


class AdvertisingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.advertising"

    def ready(self):
        from . import jobs  # noqa: F401  (registers job types and schedules)
