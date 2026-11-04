import secrets

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import MAX_BODY, MAX_SUBJECT, Ticket


def new_submission_key():
    return secrets.token_urlsafe(24)


class _Submitted(forms.Form):
    """Carries a one-time key so a repeated or retried submission is applied once."""

    submission_key = forms.CharField(widget=forms.HiddenInput, max_length=64)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("submission_key", new_submission_key())


class TicketForm(_Submitted):
    subject = forms.CharField(label=_("Subject"), min_length=5, max_length=MAX_SUBJECT)
    category = forms.ChoiceField(label=_("Category"), choices=[("", _("Choose a category"))] + Ticket.Category.choices)
    organization = forms.ChoiceField(label=_("About which advertiser account?"), required=False)
    description = forms.CharField(label=_("How can we help?"), min_length=10, max_length=MAX_BODY,
                                  widget=forms.Textarea(attrs={"rows": 8}),
                                  help_text=_("Describe what happened and what you expected. Never include your "
                                              "password, PIN or payment details. Attachments are not supported."))

    def __init__(self, *args, organizations=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.organizations = {str(org.pk): org for org in organizations}
        if self.organizations:
            self.fields["organization"].choices = [("", _("My own account (not an advertiser)"))] + [
                (key, org.display_name) for key, org in self.organizations.items()]
            self.order_fields(["subject", "category", "organization", "description"])
        else:
            del self.fields["organization"]

    def clean_organization(self):
        value = self.cleaned_data.get("organization") or ""
        if value and value not in self.organizations:
            raise forms.ValidationError(_("Choose one of your advertiser accounts."))
        return self.organizations.get(value)


class ReplyForm(_Submitted):
    body = forms.CharField(label=_("Your reply"), min_length=2, max_length=MAX_BODY,
                           widget=forms.Textarea(attrs={"rows": 5}))


class StaffReplyForm(_Submitted):
    THEN_CHOICES = [
        ("", _("Keep the current status")),
        (Ticket.Status.WAITING, _("Waiting for customer")),
        (Ticket.Status.RESOLVED, _("Resolved")),
    ]
    body = forms.CharField(label=_("Message"), min_length=2, max_length=MAX_BODY,
                           widget=forms.Textarea(attrs={"rows": 5}))
    internal = forms.BooleanField(label=_("Internal note (staff only, never shown to the customer)"), required=False)
    then_status = forms.ChoiceField(label=_("Then"), choices=THEN_CHOICES, required=False)
