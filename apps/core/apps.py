from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.core'

    def ready(self):
        from . import checks  # noqa: F401  (registers system checks)
        from .jobs import builtin  # noqa: F401  (registers built-in job types)
