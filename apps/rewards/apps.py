from django.apps import AppConfig


class RewardsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.rewards"

    def ready(self):
        from apps.viewing.services import session_reversed

        from . import jobs, services  # noqa: F401  (job types and schedules)

        session_reversed.connect(services.on_session_reversed, dispatch_uid="rewards.reverse_on_session_reversed")
