"""PAY-UX-01: member-facing names for the manual payment states.

Presentation only: each label names one real state of ManualMembershipPayment (or, for
"Awaiting payment", a pending membership with no open claim). No state is added or inferred;
"under review" is not a separate state, so it is never shown as one.
"""

from django import template
from django.utils.translation import gettext_lazy as _

from apps.payments.models import ManualMembershipPayment

register = template.Library()

Status = ManualMembershipPayment.Status
AWAITING_PAYMENT = _("Awaiting payment")
MEMBER_LABELS = {
    Status.PENDING_VERIFICATION: _("Verification requested"),
    Status.APPROVED: _("Membership active"),
    Status.REJECTED: _("Verification unsuccessful"),
    Status.CANCELLED: _("Withdrawn"),
}


@register.filter
def claim_status(claim):
    """The member-facing label of a claim's state; unknown states fall back to the model's label."""
    return MEMBER_LABELS.get(claim.status, claim.get_status_display())


@register.simple_tag
def awaiting_payment_label():
    return AWAITING_PAYMENT
