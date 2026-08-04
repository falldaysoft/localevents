from django import forms
from django.utils import timezone

from events.models import Category

INPUT_CLASS = (
    "w-full rounded-lg border border-slate-300 px-3 py-2 text-sm "
    "focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500"
)

# How many blank date rows to offer beyond the ones already filled in. Two is
# enough to add a date without thinking about it; a submitter with more than
# that presses "Add more dates", which is one round trip and no JavaScript.
EXTRA_OCCURRENCE_ROWS = 2

# The ceiling on a single submission's dates. A weekly market runs indefinitely
# and nobody should be typing a hundred rows — that is a recurrence rule, and
# until there is one, a series is listed for the run the submitter can be
# bothered to enter and then renewed.
MAX_OCCURRENCE_ROWS = 60


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

    is_series = forms.BooleanField(
        required=False,
        label="This repeats regularly",
        help_text="Weekly classes, clubs and markets. A moderator will decide "
        "how it's listed.",
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


class OccurrenceForm(forms.Form):
    """One date this event happens on, with its own hours.

    Every date is a row of the same shape — there is no privileged "first"
    date carrying the only end time. That asymmetry is what a farmers market
    open Fridays 9–2 and Saturdays 7–2 breaks: whichever day came second lost
    its hours entirely, and the submitter had no field to put them back in.

    An end that lands on a later day is not a special case here. A festival
    running Friday evening to Sunday afternoon is one row, and everything
    downstream reads it as a span rather than an instant.
    """

    start = forms.DateTimeField(
        label="Starts",
        widget=forms.DateTimeInput(
            attrs={"class": INPUT_CLASS, "type": "datetime-local"},
            format="%Y-%m-%dT%H:%M",
        ),
    )
    end = forms.DateTimeField(
        label="Ends", required=False,
        widget=forms.DateTimeInput(
            attrs={"class": INPUT_CLASS, "type": "datetime-local"},
            format="%Y-%m-%dT%H:%M",
        ),
        help_text="Leave blank if it runs until it's done. May be a later day.",
    )
    note = forms.CharField(
        label="Note", max_length=200, required=False,
        widget=forms.TextInput(attrs={"class": INPUT_CLASS}),
        help_text="Anything specific to this date, e.g. “Guest speaker”.",
    )

    def clean_start(self):
        return _aware(self.cleaned_data["start"])

    def clean_end(self):
        return _aware(self.cleaned_data.get("end"))

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start"), cleaned.get("end")
        if start and end and end <= start:
            self.add_error("end", "The end time must be after the start.")
        return cleaned


def _aware(value):
    if value is not None and timezone.is_naive(value):
        return timezone.make_aware(value)
    return value


class BaseOccurrenceFormSet(forms.BaseFormSet):
    """The dates, checked as a set.

    The wrong-year guard lives here rather than on a single field because the
    mistake it catches is not per-row. Both live extractions that got the year
    wrong got it uniformly wrong — every date a year in the past — while a
    series being resubmitted after a moderator's question legitimately has
    dates that have already gone by. Rejecting each past row individually
    would block the second case to catch the first; requiring that *something*
    is still ahead catches the first and leaves the second alone.
    """

    def add_fields(self, form, index):
        super().add_fields(form, index)
        if self.can_delete and "DELETE" in form.fields:
            # "Delete" beside a date reads as deleting the event. It removes
            # one date from the listing, which is a much smaller thing.
            form.fields["DELETE"].label = "Remove this date"

    def clean(self):
        super().clean()
        if any(self.errors):
            return

        dated = [
            form.cleaned_data["start"]
            for form in self.forms
            if form.cleaned_data.get("start")
            and not form.cleaned_data.get("DELETE")
        ]

        if not dated:
            raise forms.ValidationError("Give at least one date.")

        if len(dated) != len(set(dated)):
            raise forms.ValidationError(
                "The same date and time is listed twice."
            )

        if max(dated) < timezone.now() - timezone.timedelta(days=1):
            raise forms.ValidationError(
                "That date has already passed. Is it the right year?"
                if len(dated) == 1
                else "Those dates have all passed. Is it the right year?"
            )

    def dates(self):
        """The kept rows, soonest first, as plain dicts."""
        rows = [
            {
                "start": form.cleaned_data["start"],
                "end": form.cleaned_data.get("end"),
                "note": form.cleaned_data.get("note", ""),
            }
            for form in self.forms
            if form.cleaned_data.get("start")
            and not form.cleaned_data.get("DELETE")
        ]
        return sorted(rows, key=lambda row: row["start"])


OccurrenceFormSet = forms.formset_factory(
    OccurrenceForm,
    formset=BaseOccurrenceFormSet,
    extra=EXTRA_OCCURRENCE_ROWS,
    max_num=MAX_OCCURRENCE_ROWS,
    can_delete=True,
    can_delete_extra=False,
)
