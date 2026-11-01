from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.accounts'

    def ready(self):
        from . import checks  # noqa: F401  (registers system checks)
        from .emails import register_kinds

        register_kinds()
