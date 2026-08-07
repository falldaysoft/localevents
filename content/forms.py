"""The two forms a moderator sees: write a page, add an image.

Styling and the access rule are both borrowed from `moderation` rather than
restated. These screens sit inside `/moderate/` and should not look or behave
like a second, slightly different admin.
"""

from django import forms
from django.utils.text import slugify

from moderation.forms import INPUT_CLASS

from . import images
from .models import Image, Page

CHECKBOX_CLASS = "h-4 w-4 rounded border-slate-300"


class PageForm(forms.ModelForm):
    class Meta:
        model = Page
        fields = [
            "title",
            "slug",
            "body",
            "is_published",
            "show_in_footer",
            "footer_order",
        ]
        widgets = {
            "title": forms.TextInput(attrs={"class": INPUT_CLASS}),
            "slug": forms.TextInput(
                attrs={"class": INPUT_CLASS, "placeholder": "from the title"}
            ),
            "body": forms.Textarea(
                attrs={"class": INPUT_CLASS, "rows": 22, "spellcheck": "true"}
            ),
            "footer_order": forms.NumberInput(attrs={"class": INPUT_CLASS}),
            "is_published": forms.CheckboxInput(attrs={"class": CHECKBOX_CLASS}),
            "show_in_footer": forms.CheckboxInput(attrs={"class": CHECKBOX_CLASS}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Blank means "derive it from the title", which `Page.save` does. The
        # model field is not nullable, so this has to be relaxed on the form
        # rather than on the column.
        self.fields["slug"].required = False

    def clean_slug(self):
        """Only ever normalise what was typed; never invent one here.

        A slug the author left blank is filled in at save, where the collision
        rule lives. Doing it here as well would mean two places deciding what a
        page's address is.
        """
        slug = (self.cleaned_data.get("slug") or "").strip()
        if not slug:
            return ""

        slug = slugify(slug)
        clash = Page.objects.filter(slug=slug).exclude(pk=self.instance.pk).exists()
        if clash:
            raise forms.ValidationError(
                "Another page already uses that address. Pick a different one."
            )
        return slug


class ImageUploadForm(forms.ModelForm):
    # A plain FileField, deliberately not an ImageField. ImageField runs its own
    # Pillow check first and fails with "Upload a valid image" — which would
    # shadow every specific refusal `content.images` makes, so an SVG or a HEIC
    # would get a generic shrug instead of the sentence explaining what to do.
    # One gate, and it is the one that knows why.
    file = forms.FileField(
        label="Image file",
        help_text="JPEG, PNG, GIF or WebP. Large photos are resized automatically.",
        widget=forms.ClearableFileInput(attrs={"accept": "image/*"}),
    )

    class Meta:
        model = Image
        fields = ["title", "alt_text"]
        widgets = {
            "title": forms.TextInput(attrs={"class": INPUT_CLASS}),
            "alt_text": forms.TextInput(attrs={"class": INPUT_CLASS}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The uploader has usually just picked "IMG_4821.HEIC"; asking them to
        # also invent a title before the upload will even be accepted is a
        # pointless gate. `clean` falls back to the filename.
        self.fields["title"].required = False

    def clean_file(self):
        """Normalise here, so an unusable upload fails as a field error.

        Running the real work at clean time means a refusal lands next to the
        file input with an explanation, rather than becoming a 500 further down
        — and it means `save` cannot be reached with an image that was never
        checked.
        """
        upload = self.cleaned_data["file"]
        try:
            self._normalised = images.normalise(upload)
        except images.ImageRejected as exc:
            raise forms.ValidationError(str(exc)) from exc
        return upload

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("title"):
            upload = cleaned.get("file")
            if upload:
                stem = upload.name.rsplit(".", 1)[0].replace("_", " ").strip()
                cleaned["title"] = stem[:200] or "Untitled image"
        return cleaned

    def save(self, commit=True):
        image = super().save(commit=False)
        image.title = self.cleaned_data["title"]
        image.apply(self._normalised, filename=self.cleaned_data["file"].name)
        if commit:
            image.save()
        return image


class ImageDetailsForm(forms.ModelForm):
    """Title and alt text only.

    The pixels are deliberately not editable. Replacing the bytes under a
    stable URL would leave every page that embeds it showing something its
    author never chose, and caches holding the old one for a week either way.
    Uploading a new image and changing the page is the honest path.
    """

    class Meta:
        model = Image
        fields = ["title", "alt_text"]
        widgets = {
            "title": forms.TextInput(attrs={"class": INPUT_CLASS}),
            "alt_text": forms.TextInput(attrs={"class": INPUT_CLASS}),
        }
