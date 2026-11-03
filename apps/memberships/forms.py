from django import forms
from django.utils.translation import gettext_lazy as _

from .entitlements import ENTITLEMENTS
from .models import MAX_BENEFIT_LENGTH, MAX_BENEFITS, MembershipPlan, Subscription
from .services import ADMIN_REASONS, GRANT_REASON_MAX, GRANT_REASON_MIN


class MembershipPlanForm(forms.ModelForm):
    benefits_text = forms.CharField(
        label=_("Benefits"), required=False, widget=forms.Textarea(attrs={"rows": 5}),
        help_text=_("One benefit per line (at most %(n)s).") % {"n": MAX_BENEFITS},
    )
    entitlement_codes = forms.MultipleChoiceField(
        label=_("Entitlements"), required=False, widget=forms.CheckboxSelectMultiple,
        choices=list(ENTITLEMENTS.items()),
        help_text=_("What this plan unlocks. Video access checks use these."),
    )

    class Meta:
        model = MembershipPlan
        fields = ["code", "name", "description", "price_amount", "duration_days", "display_order", "is_public"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial["benefits_text"] = "\n".join(self.instance.benefits)
            self.initial["entitlement_codes"] = list(self.instance.entitlements)

    def clean_benefits_text(self):
        lines = [line.strip() for line in self.cleaned_data["benefits_text"].splitlines() if line.strip()]
        if len(lines) > MAX_BENEFITS or any(len(line) > MAX_BENEFIT_LENGTH for line in lines):
            raise forms.ValidationError(
                _("List at most %(n)s benefits of %(len)s characters each.")
                % {"n": MAX_BENEFITS, "len": MAX_BENEFIT_LENGTH})
        return lines

    def _post_clean(self):
        self.instance.benefits = self.cleaned_data.get("benefits_text", [])
        self.instance.entitlements = self.cleaned_data.get("entitlement_codes", [])
        super()._post_clean()

    def plan_values(self):
        values = {name: self.cleaned_data[name] for name in
                  ("name", "description", "price_amount", "duration_days", "display_order", "is_public")}
        values["benefits"] = self.cleaned_data["benefits_text"]
        values["entitlements"] = self.cleaned_data["entitlement_codes"]
        return values


class ComplimentaryGrantForm(forms.Form):
    """Console form for a time-limited complimentary demo membership. Validation of
    the expiry, reason and eligibility is repeated by services.grant_complimentary."""

    DATETIME_FORMAT = "%Y-%m-%dT%H:%M"

    plan = forms.ModelChoiceField(
        label=_("Membership plan"), queryset=MembershipPlan.objects.none(), empty_label=_("Choose a plan"),
    )
    expires_at = forms.DateTimeField(
        label=_("Access ends at"), input_formats=[DATETIME_FORMAT],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format=DATETIME_FORMAT),
        help_text=_("In the site time zone. Must be in the future and within the plan's own length."),
    )
    reason = forms.CharField(
        label=_("Reason for the complimentary grant"), min_length=GRANT_REASON_MIN, max_length=GRANT_REASON_MAX,
        widget=forms.Textarea(attrs={"rows": 3}), help_text=_("Recorded in the audit trail."),
    )
    operation = forms.CharField(widget=forms.HiddenInput, max_length=64)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plan"].queryset = MembershipPlan.objects.filter(status=MembershipPlan.Status.ACTIVE)


class ComplimentaryConfirmForm(ComplimentaryGrantForm):
    confirm = forms.BooleanField(
        label=_("I confirm this is a complimentary demo membership: no payment was received or simulated."),
    )


class ReasonForm(forms.Form):
    reason = forms.ChoiceField(
        label=_("Reason"),
        choices=[(value, label) for value, label in Subscription.Reason.choices if value in ADMIN_REASONS],
    )
