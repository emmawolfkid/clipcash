from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.accounts'

    def ready(self):
        from . import checks  # noqa: F401  (registers system checks)
        from django.db.models.signals import post_migrate

        from .emails import register_kinds
        from .roles import sync_role_groups

        register_kinds()
        post_migrate.connect(sync_role_groups, dispatch_uid="accounts.sync_role_groups")
