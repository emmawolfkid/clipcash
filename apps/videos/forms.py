from django import forms
from django.utils.translation import gettext_lazy as _

from .models import Video, VideoCategory


class VideoForm(forms.ModelForm):
    class Meta:
        model = Video
        fields = ["title", "slug", "description", "category", "required_entitlement", "duration_seconds"]
        help_texts = {"slug": _("Leave blank to generate from the title. Fixed once published.")}
        widgets = {"description": forms.Textarea(attrs={"rows": 5})}

    def __init__(self, *args, demo=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["slug"].required = False
        current = self.instance.category_id if self.instance.pk else None
        self.fields["category"].queryset = VideoCategory.objects.filter(is_active=True) | (
            VideoCategory.objects.filter(pk=current) if current else VideoCategory.objects.none())
        if demo:
            self.fields["duration_seconds"].help_text = _("Demo videos only: shown in the library.")
        else:
            del self.fields["duration_seconds"]  # Bunny reports the real duration.

    def _post_clean(self):
        # Model validation runs in the service; slug may be blank here.
        pass

    def video_values(self):
        return {name: self.cleaned_data.get(name) for name in self.fields}


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
