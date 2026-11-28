from django import forms
from django.utils.translation import gettext_lazy as _

from .models import Video, VideoCategory


class VideoForm(forms.ModelForm):
    class Meta:
        model = Video
        fields = ["title", "slug", "description", "category", "required_entitlement", "duration_seconds"]
        help_texts = {"slug": _("Leave blank to generate from the title. Fixed once published.")}
        widgets = {"description": forms.Textarea(attrs={"rows": 5})}

    def __init__(self, *args, demo=False, local=False, upload=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["slug"].required = False
        current = self.instance.category_id if self.instance.pk else None
        self.fields["category"].queryset = VideoCategory.objects.filter(is_active=True) | (
            VideoCategory.objects.filter(pk=current) if current else VideoCategory.objects.none())
        self._plan_names()
        if demo:
            self.fields["duration_seconds"].help_text = _("Demo videos only: shown in the library.")
        elif local:
            self.fields["duration_seconds"].help_text = _("Filled in from the file when possible. Enter it only if "
                                                          "it is missing.")
        else:
            del self.fields["duration_seconds"]  # Bunny reports the real duration.
        if upload:  # OPS-01: one step for local storage: details and the file together.
            self.fields["media"] = forms.FileField(
                label=_("Video file (MP4)"), required=True,
                help_text=_("A compressed MP4 (H.264 video, AAC sound) plays in every browser."),
                widget=forms.ClearableFileInput(attrs={"accept": "video/mp4,.mp4", "data-local-video": ""}))
            self.fields["thumbnail"] = forms.FileField(
                label=_("Thumbnail image (optional)"), required=False,
                help_text=_("JPEG or PNG, up to 2 MB."),
                widget=forms.ClearableFileInput(attrs={"accept": "image/jpeg,image/png"}))
            self.fields["reported_duration"] = forms.CharField(required=False, widget=forms.HiddenInput(
                attrs={"data-local-duration": ""}))

    def _plan_names(self):
        """OPS-01: show which membership plans can watch each access level."""
        from apps.memberships.models import MembershipPlan

        plans = list(MembershipPlan.objects.exclude(status=MembershipPlan.Status.ARCHIVED).order_by("display_order")
                     .values_list("name", "entitlements"))
        choices = []
        for code, label in self.fields["required_entitlement"].choices:
            names = [name for name, entitlements in plans if code in (entitlements or [])]
            choices.append((code, f"{label} — {', '.join(names)}" if names else f"{label} — {_('no plan yet')}"))
        self.fields["required_entitlement"].choices = choices
        self.fields["required_entitlement"].label = _("Who can watch (membership plans)")

    def _post_clean(self):
        # Model validation runs in the service; slug may be blank here.
        pass

    def video_values(self):
        return {name: self.cleaned_data.get(name) for name in self.fields
                if name not in ("media", "thumbnail", "reported_duration")}


class CategoryForm(forms.ModelForm):
    class Meta:
        model = VideoCategory
        fields = ["name", "slug", "description", "display_order"]
        help_texts = {"slug": _("Leave blank to generate from the name.")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["slug"].required = False

    def _post_clean(self):
        pass

    def category_values(self):
        return {name: self.cleaned_data.get(name) for name in self.fields}
