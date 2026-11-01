"""
Grant a complimentary (free, explicitly labelled) membership.

Requires DJANGO_MEMBERSHIPS_COMPLIMENTARY_GRANTS=True and an ACTIVE Super
Administrator as the acting account. Recorded with amount paid 0 and audited.
It never marks anything as paid. ``--dev-period-minutes`` (development only,
DEBUG=True) creates a short membership for testing expiry.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.audit.context import audit_scope
from apps.memberships import services
from apps.memberships.models import MembershipPlan


class Command(BaseCommand):
    help = "Grant a complimentary membership (Super Administrator, audited, no payment)."

    def add_arguments(self, parser):
        parser.add_argument("--actor-email", required=True, help="Super Administrator performing the grant.")
        parser.add_argument("--member-email", required=True)
        parser.add_argument("--plan-code", required=True)
        parser.add_argument("--dev-period-minutes", type=int, default=None,
                            help="Development only (DEBUG=True): membership length in minutes.")

    def handle(self, *args, actor_email, member_email, plan_code, dev_period_minutes, **options):
        User = get_user_model()
        try:
            actor = User.objects.get_by_natural_key(actor_email)
            member = User.objects.get_by_natural_key(member_email)
            plan = MembershipPlan.objects.get(code=plan_code)
        except (User.DoesNotExist, MembershipPlan.DoesNotExist) as error:
            raise CommandError(str(error)) from error
        period = timedelta(minutes=dev_period_minutes) if dev_period_minutes else None
        with audit_scope(source="CLI"):
            try:
                subscription = services.grant_complimentary(actor, member, plan, period=period)
            except services.MembershipError as error:
                raise CommandError(f"Refused: {error} ({error.code})") from error
        self.stdout.write(f"Complimentary {plan.code} membership for {member.email}: "
                          f"{subscription.starts_at:%Y-%m-%d %H:%M} to {subscription.expires_at:%Y-%m-%d %H:%M}.")
