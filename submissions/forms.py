from django import forms
from django.utils import timezone

from events.models import Category

INPUT_CLASS = (
    "w-full rounded-lg border border-slate-300 px-3 py-2 text-sm "
    "focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500"
)


class StartSubmissionForm(forms.Form):
    """Step one: a link, or a decision to type it in by hand.

    A URL is not required. Plenty of community events exist only as a poster in
    a window, and refusing those would quietly exclude exactly the small,
    local things this site is for.
    """

    source_url = forms.URLField(
        required=False,
        label="Link to the event",
        widget=forms.URLInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "https://…",
                "autofocus": "autofocus",
            }
        ),
        help_text="We'll read the page and fill in what we can.",
    )
    manual = forms.BooleanField(required=False, widget=forms.HiddenInput)

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("source_url") and not cleaned.get("manual"):
            raise forms.ValidationError(
                "Paste a link, or choose to enter the details yourself."
            )
        return cleaned


class EventDraftForm(forms.Form):
    """Step two: the submitter checks what we found.

    Everything is editable. The extraction is a starting point, and the person
    who submitted the link is the one best placed to correct it — which is the
    reason this step exists at all.
    """

    title = forms.CharField(
        max_length=250, widget=forms.TextInput(attrs={"class": INPUT_CLASS})
    )
    summary = forms.CharField(
        max_length=300, required=False,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS}),
        help_text="One line, shown on listing cards.",
    )
    description = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": INPUT_CLASS, "rows": 6}),
    )

    venue_name = forms.CharField(
        max_length=200, required=False,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS}),
        help_text="Leave blank if the location isn't settled yet.",
    )
    venue_address = forms.CharField(
        max_length=300, required=False,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS}),
    )
    venue_city = forms.CharField(
        max_length=120, required=False,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS}),
    )
    organizer_name = forms.CharField(
        max_length=200, required=False,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS}),
    )

    starts_at = forms.DateTimeField(
        label="Starts",
        widget=forms.DateTimeInput(
            attrs={"class": INPUT_CLASS, "type": "datetime-local"},
            format="%Y-%m-%dT%H:%M",
        ),
    )
    ends_at = forms.DateTimeField(
        label="Ends", required=False,
        widget=forms.DateTimeInput(
            attrs={"class": INPUT_CLASS, "type": "datetime-local"},
            format="%Y-%m-%dT%H:%M",
        ),
    )
    is_series = forms.BooleanField(
        required=False,
        label="This repeats regularly",
        help_text="Weekly classes, clubs and markets. A moderator will decide "
        "how it's listed.",
    )
    additional_dates = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": INPUT_CLASS, "rows": 3}),
        help_text="Other dates, one per line, as YYYY-MM-DD HH:MM.",
    )

    is_free = forms.BooleanField(required=False, label="Free to attend")
    price_note = forms.CharField(
        max_length=120, required=False,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS}),
        help_text="e.g. “$10 at the door” or “Pay what you can”.",
    )
    is_family_friendly = forms.BooleanField(required=False)
    accessibility_notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": INPUT_CLASS, "rows": 2}),
    )

    categories = forms.ModelMultipleChoiceField(
        queryset=Category.objects.filter(is_active=True),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    source_url = forms.URLField(
        required=False, widget=forms.URLInput(attrs={"class": INPUT_CLASS})
    )
    ticket_url = forms.URLField(
        required=False, widget=forms.URLInput(attrs={"class": INPUT_CLASS})
    )

    # Only rendered once a moderator has asked something. Carried on this form
    # rather than a separate one so answering the question and correcting the
    # details are a single action — two forms would let someone reply and
    # think they had resubmitted.
    reply = forms.CharField(
        required=False,
        label="Your reply to the moderator",
        widget=forms.Textarea(attrs={"class": INPUT_CLASS, "rows": 3}),
    )

    def clean_starts_at(self):
        starts_at = self.cleaned_data["starts_at"]
        if timezone.is_naive(starts_at):
            starts_at = timezone.make_aware(starts_at)
        if starts_at < timezone.now() - timezone.timedelta(days=1):
            raise forms.ValidationError(
                "That date has already passed. Is it the right year?"
            )
        return starts_at

    def clean(self):
        cleaned = super().clean()
        starts_at, ends_at = cleaned.get("starts_at"), cleaned.get("ends_at")
        if starts_at and ends_at:
            if timezone.is_naive(ends_at):
                ends_at = timezone.make_aware(ends_at)
                cleaned["ends_at"] = ends_at
            if ends_at <= starts_at:
                self.add_error("ends_at", "The end time must be after the start.")
        return cleaned

    def clean_additional_dates(self):
        """Parse the extra dates, reporting the line that failed.

        Series submissions routinely carry a dozen dates; a blanket "invalid
        format" would leave the submitter hunting for which one.
        """
        raw = self.cleaned_data.get("additional_dates", "")
        parsed = []
        for number, line in enumerate(raw.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
                try:
                    value = timezone.datetime.strptime(line, fmt)
                except ValueError:
                    continue
                parsed.append(timezone.make_aware(value))
                break
            else:
                raise forms.ValidationError(
                    f"Line {number} (“{line}”) isn't a date we recognise. "
                    "Use YYYY-MM-DD HH:MM."
                )
        return parsed
