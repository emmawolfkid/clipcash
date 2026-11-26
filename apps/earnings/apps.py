from django.apps import AppConfig


class EarningsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.earnings"
    verbose_name = "Real rewards and campaign funding"

    def ready(self):
        from apps.viewing.services import session_closed, session_reversed

        from . import jobs, services  # noqa: F401  (job types and schedules)

        session_closed.connect(services.on_session_closed, dispatch_uid="earnings.settle_on_session_closed")
        session_reversed.connect(services.on_session_reversed, dispatch_uid="earnings.reverse_on_session_reversed")
