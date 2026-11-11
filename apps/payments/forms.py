"""PAY-01 forms. Field values are only collected here; every rule is enforced again by
apps.payments.manual, which is the single authority."""

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import ManualMembershipPayment


class ManualPaymentForm(forms.Form):
    amount = forms.CharField(
        label=_("Amount paid (TZS)"), max_length=20,
        help_text=_("Exactly the plan price shown above, in whole shillings."),
        widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off"}))
    payer_phone = forms.CharField(
        label=_("Phone number you paid from"), max_length=24,
        help_text=_("For example 0781234567 or +255781234567."),
        widget=forms.TextInput(attrs={"type": "tel", "autocomplete": "tel", "inputmode": "tel"}))
    transaction_reference = forms.CharField(
        label=_("Airtel transaction reference"), max_length=60,
        help_text=_("Copy it from the Airtel Money confirmation SMS, for example the code after “TID” or "
                    "“Ref”."),
        widget=forms.TextInput(attrs={"autocomplete": "off", "autocapitalize": "characters", "spellcheck": "false"}))
    paid_on = forms.DateField(
        label=_("Payment date (optional)"), required=False,
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))


class ApproveManualPaymentForm(forms.Form):
    verified_amount = forms.CharField(
        label=_("Amount you found in the Airtel business records (TZS)"), max_length=20,
        widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off"}))
    verified_reference = forms.CharField(
        label=_("Transaction reference you found in the Airtel business records"), max_length=60,
        widget=forms.TextInput(attrs={"autocomplete": "off", "spellcheck": "false"}))
    receipt_confirmed = forms.BooleanField(
        label=_("I found this payment in the official Airtel business records, it was received into ClipCash's "
                "Lipa account, and it has not been used for another membership."))
    note = forms.CharField(label=_("Note (optional)"), max_length=300, required=False)


class RejectManualPaymentForm(forms.Form):
    reason = forms.ChoiceField(label=_("Reason (shown to the member)"),
                               choices=[("", _("Choose a reason"))] + ManualMembershipPayment.RejectionReason.choices)
    note = forms.CharField(label=_("Note to the member"), max_length=300, required=False,
                           help_text=_("Required for “Other”. Never include personal data of other people."))


class PaymentInstructionForm(forms.Form):
    business_number = forms.CharField(
        label=_("Airtel Lipa business number"), max_length=20,
        widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off"}))
    recipient_name = forms.CharField(label=_("Recipient name (as Airtel shows it when paying)"), max_length=100)
    instructions = forms.CharField(
        label=_("Payment steps shown to members (optional)"), max_length=1000, required=False,
        widget=forms.Textarea(attrs={"rows": 5}))
