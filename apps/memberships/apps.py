from django.apps import AppConfig


class MembershipsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.memberships'

    def ready(self):
        from . import jobs  # noqa: F401  (registers the expiry job and schedule)


