from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.notifications'

    def ready(self):
        from . import checks, inbox, jobs, services  # noqa: F401  (checks, email kinds, job types and schedules)
