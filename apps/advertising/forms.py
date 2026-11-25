from django import forms
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.memberships.models import MembershipPlan
from apps.videos.models import Video

from . import services
from .models import AdvertiserMembership, Campaign, Organization

DATETIME_FORMAT = "%Y-%m-%dT%H:%M"


class OrganizationForm(forms.ModelForm):
    class Meta:
        model = Organization
        fields = list(services.ORG_FIELDS)
        help_texts = {
            "registration_number": _("Optional. Shown only to your team and ClipCash reviewers."),
            "tax_id": _("Optional. Shown only to your team and ClipCash reviewers."),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.is_approved:
            for name in services.ORG_IDENTITY_FIELDS:
                self.fields[name].disabled = True
                self.fields[name].help_text = _("Verified details. Contact support to change them.")

    def _post_clean(self):
        pass  # Model validation runs in the service.

    def values(self):
        return {name: self.cleaned_data.get(name) for name in self.fields if not self.fields[name].disabled}


class CampaignForm(forms.ModelForm):
    starts_at = forms.DateTimeField(label=_("Starts"), input_formats=[DATETIME_FORMAT],
                                    widget=forms.DateTimeInput(attrs={"type": "datetime-local"},
                                                               format=DATETIME_FORMAT))
    ends_at = forms.DateTimeField(label=_("Ends"), input_formats=[DATETIME_FORMAT],
                                  widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format=DATETIME_FORMAT),
                                  help_text=_("Times are in the platform timezone."))
    target_plans = forms.ModelMultipleChoiceField(
        label=_("Membership plans"), required=False, widget=forms.CheckboxSelectMultiple,
        queryset=MembershipPlan.objects.none(),
        help_text=_("Leave all unticked to show the campaign to every member with an active membership."))

    class Meta:
        model = Campaign
        fields = ["name", "description", "video", "starts_at", "ends_at", "target_plans", "max_views_per_member",
                  "demo_budget_amount", "demo_reward_per_view"]
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}
        help_texts = {
            "max_views_per_member": _("1 to 10. How many verified views one member can count for this campaign."),
            "demo_budget_amount": _("Demonstration figure only. It is not money and is never charged or paid out."),
            "demo_reward_per_view": _("Demonstration figure only. No rewards are paid yet."),
        }

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        allowed = Q(pk__in=services.granted_videos(organization).values("pk"))
        if self.instance.pk:
            allowed |= Q(pk=self.instance.video_id)  # Keep showing the current choice.
        self.fields["video"].queryset = Video.objects.filter(allowed).order_by("title")
        self.fields["video"].help_text = _("Videos ClipCash has authorized for your advertiser account.")
        self.fields["target_plans"].queryset = MembershipPlan.objects.filter(
            status=MembershipPlan.Status.ACTIVE).order_by("display_order", "name")
        self.fields["max_views_per_member"].min_value = 1
        self.fields["max_views_per_member"].max_value = 10

    def _post_clean(self):
        pass

    def values(self):
        data = {name: self.cleaned_data.get(name) for name in self.fields if name != "target_plans"}
        return data, list(self.cleaned_data.get("target_plans") or [])


class InviteForm(forms.Form):
    email = forms.EmailField(label=_("Email address"))
    role = forms.ChoiceField(label=_("Role"), choices=AdvertiserMembership.Role.choices,
                             initial=AdvertiserMembership.Role.MANAGER)


class ReasonForm(forms.Form):
    reason = forms.CharField(label=_("Reason"), max_length=300, required=False,
                             widget=forms.Textarea(attrs={"rows": 2}))


class StaffCampaignForm(CampaignForm):
    """UI-DASH-03: the advertiser campaign form for staff acting for an organization. Same fields
    and validation (the service re-checks everything); staff also choose the organization and may
    pick any published, ready video (the view authorizes it for the organization if needed)."""

    organization = forms.ModelChoiceField(label=_("Advertiser"), queryset=Organization.objects.none(),
                                          help_text=_("The client organization this campaign belongs to."))

    field_order = ["organization", "name", "description", "video", "starts_at", "ends_at", "target_plans",
                   "max_views_per_member", "demo_budget_amount", "demo_reward_per_view"]

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, organization=organization, **kwargs)
        V = Organization.Verification
        self.fields["organization"].queryset = Organization.objects.filter(
            verification_status__in=[V.APPROVED, V.PENDING]).order_by("display_name")
        if organization is not None:
            self.fields["organization"].initial = organization.pk
        editing = not self.instance._state.adding  # UUID primary keys exist before saving.
        if editing:  # The owning organization never changes.
            self.fields["organization"].disabled = True
            self.fields["organization"].initial = self.instance.organization_id
        videos = Q(publication_status=Video.Publication.PUBLISHED, processing_status=Video.Processing.READY)
        if editing:
            videos |= Q(pk=self.instance.video_id)
        self.fields["video"].queryset = Video.objects.filter(videos).order_by("title")
        self.fields["video"].help_text = _("Published, ready videos. Upload new videos under Video management.")
        self.order_fields(self.field_order)

    def values(self):
        data, plans = super().values()
        data.pop("organization", None)
        return data, plans
