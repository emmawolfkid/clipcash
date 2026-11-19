"""PAY-LIVE-01D: Operations Console forms for managing an existing membership.

Every form needs an explicit confirmation and the administrator's reason. The services in
apps.memberships.services re-check everything; these forms only collect the input.
"""

from django import forms
from django.utils.translation import gettext_lazy as _

from . import services
from .models import Subscription

NOTE_MIN = 5


def _reason_choices():
    return [("", _("Choose a reason"))] + [
        (value, label) for value, label in Subscription.Reason.choices if value in services.ADMIN_REASONS]


class _ConfirmedForm(forms.Form):
    note = forms.CharField(
        label=_("Reason in your own words"), min_length=NOTE_MIN, max_length=services.ADMIN_NOTE_MAX,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Recorded in the audit trail with your account. Never shown to the member."))
    confirm = forms.BooleanField(label=_("I have checked this membership and want to make this change."),
                                 error_messages={"required": _("Tick the confirmation box to continue.")})


class SuspendForm(_ConfirmedForm):
    reason = forms.ChoiceField(label=_("Reason"), choices=_reason_choices)
    field_order = ["reason", "note", "confirm"]


class CancelForm(SuspendForm):
    confirm = forms.BooleanField(
        label=_("I understand cancellation ends access immediately, cannot be undone, and does not refund or "
                "reverse any payment."),
        error_messages={"required": _("Tick the confirmation box to continue.")})


class ReinstateForm(_ConfirmedForm):
    pass


class ExtensionForm(forms.Form):
    days = forms.IntegerField(
        label=_("Extend by (days)"), min_value=services.EXTENSION_MIN_DAYS, max_value=services.EXTENSION_MAX_DAYS,
        help_text=_("Between %(min)s and %(max)s days.") % {"min": services.EXTENSION_MIN_DAYS,
                                                              "max": services.EXTENSION_MAX_DAYS})
    reason = forms.CharField(
        label=_("Reason"), min_length=services.GRANT_REASON_MIN, max_length=services.GRANT_REASON_MAX,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Recorded in the audit trail and on the extension period. Never shown to the member."))
    confirm = forms.BooleanField(
        label=_("I confirm this is an administrative extension: no payment was received, and no payment, ledger "
                "or wallet record is created."),
        error_messages={"required": _("Tick the confirmation box to continue.")})
    operation = forms.CharField(widget=forms.HiddenInput, max_length=64)
    expected_end = forms.CharField(widget=forms.HiddenInput, max_length=64)
