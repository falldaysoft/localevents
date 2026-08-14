from django import forms
from django.utils import timezone

from events.forms import INPUT_CLASS, PlaceFieldsMixin
from events.models import Category

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


class EventDraftForm(PlaceFieldsMixin, forms.Form):
    """Step two: the submitter checks what we found.

    Everything is editable. The extraction is a starting point, and the person
    who submitted the link is the one best placed to correct it — which is the
    reason this step exists at all.

    The venue and organiser fields come from `PlaceFieldsMixin`, which the
    moderator's edit form uses too — the two screens ask for a place in the
    same words because they are asking for the same thing.
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

    # The guard is aimed at an extraction the submitter is being asked to
    # rubber-stamp. A moderator correcting the dates of an event that already
    # happened is doing something legitimate and is looking straight at the
    # year they typed, so their formset turns it off.
    reject_all_past = True

    # Which per-row fields, besides the start, this formset carries. Read by
    # `dates()` and by `formset_with_more_rows` so a subclass that adds a
    # column does not have to reimplement either.
    row_fields = ("end", "note")

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

        if self.reject_all_past and max(dated) < timezone.now() - timezone.timedelta(
            days=1
        ):
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
                **{name: form.cleaned_data.get(name) for name in self.row_fields},
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


def echoed_datetime(raw):
    """A submitted date, parsed so the widget can render it back.

    Handing the string straight back looks like it works and mostly does,
    because a browser posts exactly the format the input wants. But Django
    accepts several other formats on the way in, and a `datetime-local` input
    given one of them renders *empty* — so a value the server understood
    perfectly well would vanish from the page. Parsing first means the widget
    always formats it itself.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return forms.DateTimeField(required=False).clean(raw)
    except forms.ValidationError:
        # Unparseable: give it back as typed so whoever wrote it can see what
        # they wrote and correct it, rather than facing a blank box.
        return raw


def formset_with_more_rows(post, formset_class, prefix="dates"):
    """The dates as submitted, plus another batch of blank rows.

    "Add more dates" is a submit button, not JavaScript: Alpine cannot run
    under this site's CSP, so a row-adding widget would have to be hand-written
    JS, and one round trip through the server costs nothing and keeps the page
    working with scripting off entirely.

    The result is deliberately *unbound*. Rebinding would be simpler, but a
    bound form reports its errors the moment the template touches a field — so
    asking for another row would answer with a page of red complaints about the
    parts not filled in yet, and nobody asking for more space has said they are
    finished. The rows already filled in come back as initial data, so nobody
    loses their typing, and the blank count grows each time rather than
    resetting: pressing the button twice gives four empty rows, which is what
    pressing it twice ought to mean.
    """
    try:
        total = int(post.get(f"{prefix}-TOTAL_FORMS", 0))
    except (TypeError, ValueError):
        total = 0
    total = min(total, MAX_OCCURRENCE_ROWS)

    rows = []
    for index in range(total):
        start = post.get(f"{prefix}-{index}-start", "").strip()
        if not start or post.get(f"{prefix}-{index}-DELETE"):
            continue
        row = {"start": echoed_datetime(start)}
        for name in formset_class.row_fields:
            raw = post.get(f"{prefix}-{index}-{name}", "")
            # A checkbox posts "on" or nothing at all, and either is already
            # the right truthiness for a BooleanField's initial value.
            row[name] = echoed_datetime(raw) if name == "end" else raw.strip()
        rows.append(row)

    blank = max(total - len(rows), 0) + EXTRA_OCCURRENCE_ROWS
    grown = forms.formset_factory(
        formset_class.form,
        formset=formset_class,
        extra=min(blank, MAX_OCCURRENCE_ROWS - len(rows)),
        max_num=MAX_OCCURRENCE_ROWS,
        can_delete=True,
        can_delete_extra=False,
    )
    return grown(prefix=prefix, initial=rows)
