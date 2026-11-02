from django.apps import AppConfig


class WalletsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.wallets"

    def ready(self):
        from . import jobs  # noqa: F401  (job types and schedules)
