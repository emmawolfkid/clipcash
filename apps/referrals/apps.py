from django.apps import AppConfig


class ReferralsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.referrals"
    verbose_name = "Referral programme"

    def ready(self):
        from apps.viewing.services import session_closed, session_reversed

        from . import jobs, services  # noqa: F401  (job types and schedules)

        session_closed.connect(services.on_session_changed, dispatch_uid="referrals.evaluate_on_session_closed")
        session_reversed.connect(services.on_session_changed, dispatch_uid="referrals.evaluate_on_session_reversed")
