"""PAY-01 forms. Field values are only collected here; every rule is enforced again by
apps.payments.manual, which is the single authority."""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.money import format_amount

from .models import ManualMembershipPayment


class ManualPaymentForm(forms.Form):
    """PAY-UX-01: "I've paid — request verification". Every detail is optional; the amount is always
    the issued plan price, so it is not asked for."""

    quote = forms.UUIDField(required=False, widget=forms.HiddenInput)
    payer_phone = forms.CharField(
        label=_("Phone number you paid from (optional)"), max_length=24, required=False,
        help_text=_("For example 0781234567. It can be someone else's phone; it does not have to be the number "
                    "on your account."),
        widget=forms.TextInput(attrs={"type": "tel", "autocomplete": "tel", "inputmode": "tel"}))
    transaction_reference = forms.CharField(
        label=_("Transaction reference (optional)"), max_length=60, required=False,
        help_text=_("From the provider confirmation message, for example the code after “TID” or “Ref”."),
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
    """UX-01: one typed value (the provider's transaction reference, the trusted evidence that is
    checked for reuse) and three confirmations naming the exact records the reviewer checked.
    Nothing ClipCash already knows (amount due, receiving account) is retyped."""

    verified_reference = forms.CharField(
        label=_("Provider transaction ID"), max_length=60,
        help_text=_("Type it from the provider statement or business portal, not from this page."),
        error_messages={"required": _("Type the transaction ID shown in the provider records.")},
        widget=forms.TextInput(attrs={"autocomplete": "off", "spellcheck": "false", "autocapitalize": "characters"}))
    receipt_confirmed = forms.BooleanField(
        label=_("I found this payment in the official provider business records."),
        error_messages={"required": _("Confirm that you found this payment in the provider records.")})
    amount_confirmed = forms.BooleanField(
        label=_("The amount received is exactly the amount due."),
        error_messages={"required": _("Confirm that exactly the amount due was received.")})
    recipient_confirmed = forms.BooleanField(
        label=_("It was received into the account shown to the member."),
        error_messages={"required": _("Confirm that it reached the receiving account shown to the member.")})
    note = forms.CharField(label=_("Internal note (optional)"), max_length=300, required=False)

    def __init__(self, *args, instruction=None, reference_given=True, amount=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not reference_given:  # PAY-UX-01: the member gave none; the reviewer's becomes single-use.
            self.fields["verified_reference"].help_text = _(
                "The member did not give one. Find the payment by amount, time and paying number, then type its "
                "transaction ID. It is recorded on this payment and can never back another membership.")
        if amount is not None:
            self.fields["amount_confirmed"].label = _("The amount received is exactly %(amount)s.") % {
                "amount": format_amount(amount)}
        if instruction is not None:  # Name the exact account the member was shown.
            self.fields["recipient_confirmed"].label = _(
                "It was received into %(provider)s %(number)s (%(name)s).") % {
                "provider": instruction.provider_label, "number": instruction.business_number,
                "name": instruction.recipient_name}
            self.fields["receipt_confirmed"].label = _(
                "I found this payment in the official %(provider)s business records.") % {
                "provider": instruction.provider_label}


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
OTHER_PROVIDER = "OTHER"  # Form choice only; never stored (a real code is typed under Advanced).


class PaymentInstructionForm(forms.Form):
    """UX-02: add or edit one payment method. Provider first, then where members pay, then the
    optional steps; the provider code is an advanced field (presets fill it). When editing,
    ``fixed_method`` keeps the provider's code (its receipt namespace) and hides both choices.
    Every rule is checked again by apps.payments.manual when publishing."""

    preset = forms.ChoiceField(
        label=_("Provider"), required=False,
        choices=[("", _("Choose a provider"))] + list(PROVIDER_PRESETS.items()) + [(OTHER_PROVIDER, _("Other provider"))],
        help_text=_("For a bank transfer, use the bank's name below (for example “CRDB Bank transfer”) and the "
                    "account number as the receiving number."))
    provider_label = forms.CharField(
        label=_("Name shown to members"), max_length=100, required=False,
        help_text=_("Leave empty to use the provider's usual name."))
    business_number = forms.CharField(
        label=_("Receiving number"), max_length=20,
        help_text=_("Digits only: the Lipa number, till number or bank account number."),
        widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off", "spellcheck": "false"}))
    recipient_name = forms.CharField(
        label=_("Registered recipient name"), max_length=100,
        help_text=_("Exactly as the provider shows it to the person paying."),
        widget=forms.TextInput(attrs={"autocomplete": "off"}))
    instructions = forms.CharField(
        label=_("Payment steps for members (optional)"), max_length=1000, required=False,
        help_text=_("One step per line. Lines may start with “1.” or “-”, and **text** shows in bold."),
        widget=forms.Textarea(attrs={"rows": 5}))
    method = forms.CharField(
        label=_("Provider code"), max_length=20, required=False,
        help_text=_("Only for an “Other provider”: a short, permanent code such as TIGO. It cannot be changed later."),
        widget=forms.TextInput(attrs={"autocapitalize": "characters", "spellcheck": "false", "autocomplete": "off"}))

    def __init__(self, *args, fixed_method=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fixed_method = fixed_method
        if fixed_method:  # Editing: the provider stays the same provider.
            del self.fields["preset"]
            del self.fields["method"]
            self.fields["provider_label"].help_text = ""

    def clean(self):
        from . import manual

        # Every problem is reported at once, not one per submission.
        cleaned = super().clean()
        if self.fixed_method:
            method, label = self.fixed_method, (cleaned.get("provider_label") or "").strip()
        else:
            preset = cleaned.pop("preset", "") or ""
            known = preset in PROVIDER_PRESETS
            method = (cleaned.get("method") or "").strip() or (preset if known else "")  # A typed code wins.
            label = (cleaned.get("provider_label") or "").strip() or (str(PROVIDER_PRESETS[preset]) if known else "")
            if not method:
                if preset == OTHER_PROVIDER:
                    self.add_error("method", _("Enter a provider code for this provider, for example TIGO."))
                else:
                    self.add_error("preset", _("Choose a provider."))
        if method and not label and not self.has_error("provider_label"):
            self.add_error("provider_label", _("Enter the name members see for this provider."))
        try:
            cleaned.update(manual.clean_instruction_values(
                method=method, provider_label=label or method, business_number=cleaned.get("business_number", ""),
                recipient_name=cleaned.get("recipient_name", ""), instructions=cleaned.get("instructions", "")))
        except forms.ValidationError as error:
            for field, messages in error.message_dict.items():
                target = field if field in self.fields else None
                if (target and self.has_error(target)) or (field == "method" and not method) \
                        or (field == "provider_label" and not label):
                    continue  # Already explained above, or by the field itself.
                self.add_error(target, messages)
        return cleaned

    def publish_values(self):
        keys = ("method", "provider_label", "business_number", "recipient_name", "instructions")
        return {key: self.cleaned_data[key] for key in keys}


class PublishConfirmForm(forms.Form):
    """UX-02: the explicit confirmation on the review step. ``expected_active`` is the active
    version the administrator reviewed (empty when adding a provider)."""

    expected_active = forms.UUIDField(required=False, widget=forms.HiddenInput)
    details_checked = forms.BooleanField(
        label=_("I checked the receiving number and recipient name against the provider's own account."),
        error_messages={"required": _("Confirm that you checked the details against the provider's account.")})
