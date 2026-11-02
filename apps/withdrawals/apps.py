from django.apps import AppConfig


class WithdrawalsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.withdrawals"

    def ready(self):
        from . import jobs  # noqa: F401  (job types and schedules)
