"""PAY-01 forms. Field values are only collected here; every rule is enforced again by
apps.payments.manual, which is the single authority."""

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import ManualMembershipPayment


class ManualPaymentForm(forms.Form):
    quote = forms.UUIDField(required=False, widget=forms.HiddenInput)
    amount = forms.CharField(
        label=_("Amount paid (TZS)"), max_length=20,
        help_text=_("Exactly the plan price shown above, in whole shillings."),
        widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off"}))
    payer_phone = forms.CharField(
        label=_("Phone number you paid from"), max_length=24,
        help_text=_("The mobile-money number the payment came from, for example 0781234567 or +255781234567. "
                    "It does not have to be the number on your account."),
        widget=forms.TextInput(attrs={"type": "tel", "autocomplete": "tel", "inputmode": "tel"}))
    transaction_reference = forms.CharField(
        label=_("Provider transaction reference"), max_length=60,
        help_text=_("Copy it from the provider confirmation message, for example the code after “TID” or "
                    "“Ref”."),
        widget=forms.TextInput(attrs={"autocomplete": "off", "autocapitalize": "characters", "spellcheck": "false"}))
    paid_on = forms.DateField(
        label=_("Payment date (optional)"), required=False,
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    # PRD-04: checked again by content in apps.payments.evidence; the accept list only helps the browser.
    evidence = forms.FileField(
        label=_("Receipt (optional)"), required=False,
        help_text=_("A screenshot (JPEG or PNG) or PDF of the provider's confirmation, up to 5 MB. It helps our "
                    "team find your payment; the payment itself is still checked in the provider records."),
        widget=forms.ClearableFileInput(attrs={"accept": "image/jpeg,image/png,application/pdf"}))


class ApproveManualPaymentForm(forms.Form):
    verified_amount = forms.CharField(
        label=_("Amount you found in the provider business records (TZS)"), max_length=20,
        widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off"}))
    verified_reference = forms.CharField(
        label=_("Transaction reference you found in the provider business records"), max_length=60,
        widget=forms.TextInput(attrs={"autocomplete": "off", "spellcheck": "false"}))
    receipt_confirmed = forms.BooleanField(
        label=_("I found this payment in the official provider business records, and it has not been used for "
                "another membership."))
    recipient_confirmed = forms.BooleanField(label=_("It was received into the account shown to the member."))
    note = forms.CharField(label=_("Note (optional)"), max_length=300, required=False)

    verified_receiving_number = forms.CharField(label=_("Receiving number in the provider records"), max_length=15)
    verified_recipient_name = forms.CharField(label=_("Registered recipient in the provider records"), max_length=100)

    def __init__(self, *args, instruction=None, **kwargs):
        super().__init__(*args, **kwargs)
        if instruction is not None:  # Name the exact account the member was shown.
            self.fields["recipient_confirmed"].label = _(
                "It was received into %(provider)s %(number)s (%(name)s), the account shown to the member.") % {
                "provider": instruction.provider_label, "number": instruction.business_number, "name": instruction.recipient_name}


class RejectManualPaymentForm(forms.Form):
    reason = forms.ChoiceField(label=_("Reason (shown to the member)"),
                               choices=[("", _("Choose a reason"))] + ManualMembershipPayment.RejectionReason.choices)
    note = forms.CharField(label=_("Note to the member"), max_length=300, required=False,
                           help_text=_("Required for “Other”. Never include personal data of other people."))


# PRD-04: common Tanzanian channels as starting points. Codes and display names ONLY: the receiving
# number or account, the registered name and the steps are always typed by a Super Administrator.
PROVIDER_PRESETS = {
    "MPESA": _("Vodacom M-Pesa"),
    "AIRTEL_LIPA": _("Airtel Money - Lipa"),
    "MIXX_BY_YAS": _("Mixx by Yas"),
    "BANK_TRANSFER": _("Bank transfer"),
}


class PaymentInstructionForm(forms.Form):
    instruction_id = forms.UUIDField(required=False, widget=forms.HiddenInput)
    preset = forms.ChoiceField(
        label=_("Payment channel"), required=False,
        choices=[("", _("Other (enter a provider code and name below)"))] + list(PROVIDER_PRESETS.items()),
        help_text=_("Fills in the provider code and the name members see. For a bank transfer, put the bank's name "
                    "in the provider name (for example “CRDB Bank transfer”), the account number as the receiving "
                    "number and the account name as the recipient."))
    method = forms.CharField(label=_("Provider code"), max_length=20, required=False,
                             help_text=_("A stable provider code, for example TIGO. Editing details keeps this code."))
    provider_label = forms.CharField(label=_("Provider name shown to members"), max_length=100, required=False)

    business_number = forms.CharField(
        label=_("Receiving number or account number"), max_length=20,
        widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off"}))
    recipient_name = forms.CharField(label=_("Recipient name (as the provider shows it when paying)"), max_length=100)
    instructions = forms.CharField(
        label=_("Payment steps shown to members (optional)"), max_length=1000, required=False,
        widget=forms.Textarea(attrs={"rows": 5}))

    def clean(self):
        cleaned = super().clean()
        preset = cleaned.pop("preset", "") or ""
        if preset:  # A typed code or name always wins over the preset.
            cleaned["method"] = (cleaned.get("method") or "").strip() or preset
            cleaned["provider_label"] = (cleaned.get("provider_label") or "").strip() or str(PROVIDER_PRESETS[preset])
        return cleaned
