"""
Grant a time-limited complimentary (free, explicitly labelled) demo membership.

Same service and rules as the console (/manage/members/<id>/complimentary/):
requires DJANGO_MEMBERSHIPS_COMPLIMENTARY_GRANTS=True inside a restricted
demonstration, an ACTIVE Super Administrator as the acting account, an eligible
member without a current membership, a future expiry within the plan's length and
a reason. Recorded with amount paid 0 and audited. It never marks anything as paid.

    python manage.py grant_complimentary_membership --actor-email root@example.com \
        --member-email member@example.com --plan-code monthly \
        --expires-at 2026-12-01T18:00:00+03:00 --reason "Client demo, 1 December"
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.audit.context import audit_scope
from apps.memberships import services
from apps.memberships.models import MembershipPlan


class Command(BaseCommand):
    help = "Grant a time-limited complimentary demo membership (Super Administrator, audited, no payment)."

    def add_arguments(self, parser):
        parser.add_argument("--actor-email", required=True, help="Super Administrator performing the grant.")
        parser.add_argument("--member-email", required=True)
        parser.add_argument("--plan-code", required=True)
        parser.add_argument("--expires-at", required=True,
                            help="ISO 8601 date and time with a UTC offset, e.g. 2026-12-01T18:00:00+03:00.")
        parser.add_argument("--reason", required=True, help="Why the grant is made (5-200 characters).")
        parser.add_argument("--operation-id", default="",
                            help="Optional retry key (8-64 letters, digits or hyphens); repeating it returns "
                                 "the original grant.")

    def handle(self, *args, actor_email, member_email, plan_code, expires_at, reason, operation_id, **options):
        User = get_user_model()
        try:
            actor = User.objects.get_by_natural_key(actor_email)
            member = User.objects.get_by_natural_key(member_email)
            plan = MembershipPlan.objects.get(code=plan_code)
        except (User.DoesNotExist, MembershipPlan.DoesNotExist) as error:
            raise CommandError(str(error)) from error
        expiry = parse_datetime(expires_at)
        if expiry is None or timezone.is_naive(expiry):
            raise CommandError("--expires-at must be an ISO 8601 date and time with a UTC offset.")
        with audit_scope(source="CLI"):
            try:
                subscription = services.grant_complimentary(actor, member, plan, expires_at=expiry, reason=reason,
                                                            operation_id=operation_id)
            except services.MembershipError as error:
                raise CommandError(f"Refused: {error} ({error.code})") from error
            except ValidationError as error:
                raise CommandError("Refused: " + " ".join(error.messages)) from error
        self.stdout.write(f"Complimentary {plan.code} membership for {member.email}: "
                          f"{subscription.starts_at.isoformat()} to {subscription.expires_at.isoformat()} "
                          f"(no payment).")
