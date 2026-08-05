"""The three decisions, as forms — plus correcting a listing after the fact.

Each decision form carries the text that reaches the submitter, so the labels
say so plainly. A moderator writing "no" into a box marked "internal note" and
having it emailed to a stranger is the kind of surprise worth designing out.
"""

from django import forms
from django.db.models import Q

from events.models import Category, Event
from submissions.forms import (
    EXTRA_OCCURRENCE_ROWS,
    MAX_OCCURRENCE_ROWS,
    BaseOccurrenceFormSet,
    OccurrenceForm,
)

INPUT_CLASS = (
    "w-full rounded-lg border border-slate-300 px-3 py-2 text-sm "
    "focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500"
)


class ApproveForm(forms.Form):
    """Publish, and decide where it sits.

    Prominence and listing type are asked here rather than inherited from the
    submission because they *are* the editorial decision — everything else on
    the event was already checked by the person who submitted it.
    """

    prominence = forms.TypedChoiceField(
        choices=Event.Prominence.choices,
        coerce=int,
        initial=Event.Prominence.LISTED,
        widget=forms.RadioSelect,
        label="Where this sits",
    )
    listing_type = forms.ChoiceField(
        choices=Event.ListingType.choices,
        initial=Event.ListingType.ONE_OFF,
        widget=forms.RadioSelect,
        label="How it is listed",
        help_text="A series collapses to one card however many dates it has.",
    )
    categories = forms.ModelMultipleChoiceField(
        queryset=Category.objects.filter(is_active=True),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    note = forms.CharField(
        required=False,
        label="Note to the submitter",
        widget=forms.Textarea(attrs={"class": INPUT_CLASS, "rows": 3}),
        help_text="Optional. Included in the email telling them it's live.",
    )


class RejectForm(forms.Form):
    reason = forms.CharField(
        label="Why — the submitter will read this",
        widget=forms.Textarea(attrs={"class": INPUT_CLASS, "rows": 4}),
        help_text="A bare 'no' from a community site reads as arbitrary, and "
        "the person on the other end usually spent ten minutes on the form.",
    )


class RequestInfoForm(forms.Form):
    body = forms.CharField(
        label="What do you need to know?",
        widget=forms.Textarea(attrs={"class": INPUT_CLASS, "rows": 4}),
        help_text="Emailed to the submitter, who can then edit and resend.",
    )


class EventEditForm(forms.ModelForm):
    """A live listing, corrected in place.

    The moderation queue judges a submission once; this is the other half of
    the job — the typo, the wrong start time, the venue that moved, reported
    by a reader weeks after the event was approved. Sending a moderator to the
    Django admin for that would mean handing out staff accounts, which is a
    much larger grant than "may fix a listing".

    Dates are deliberately absent. They live on `Occurrence`, and changing
    *when* something happens is a different and riskier edit than fixing a
    summary — it belongs on its own screen rather than buried in twenty other
    fields where it can be changed by accident.

    Slug is absent for the same reason it is absent from the admin's editable
    set in spirit: it is the event's public URL, and quietly rewriting it
    breaks every link anyone has shared.
    """

    class Meta:
        model = Event
        fields = [
            "title", "summary", "description",
            "venue", "organizer", "categories",
            "listing_type", "prominence", "status",
            "is_free", "price_min", "price_max", "price_note",
            "age_min", "age_max", "is_family_friendly", "is_commercial",
            "accessibility_notes",
            "source_url", "ticket_url", "image_url",
            "series_ends_on",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 8}),
            "accessibility_notes": forms.Textarea(attrs={"rows": 3}),
            "listing_type": forms.RadioSelect,
            "prominence": forms.RadioSelect,
            "categories": forms.CheckboxSelectMultiple,
            "series_ends_on": forms.DateInput(attrs={"type": "date"}),
        }
        help_texts = {
            "status": "Unpublishing takes it off the site immediately.",
            "series_ends_on": "When a series stops being listed. One-off "
                              "events leave this empty.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Retired categories stay selectable *if this event already has one*.
        # Otherwise editing an unrelated field would silently strip a category
        # a moderator never touched.
        assigned = (
            self.instance.categories.values_list("pk", flat=True)
            if self.instance.pk
            else []
        )
        self.fields["categories"].queryset = Category.objects.filter(
            Q(is_active=True) | Q(pk__in=list(assigned))
        ).distinct()

        # Radios and checkboxes carry their own layout in the template; a
        # full-width input class on them looks broken.
        for field in self.fields.values():
            widget = field.widget
            if isinstance(
                widget,
                (forms.CheckboxInput, forms.CheckboxSelectMultiple, forms.RadioSelect),
            ):
                continue
            widget.attrs.setdefault("class", INPUT_CLASS)

    def clean(self):
        cleaned = super().clean()

        low, high = cleaned.get("price_min"), cleaned.get("price_max")
        if low is not None and high is not None and low > high:
            self.add_error("price_max", "The high price is below the low price.")

        young, old = cleaned.get("age_min"), cleaned.get("age_max")
        if young is not None and old is not None and young > old:
            self.add_error("age_max", "The maximum age is below the minimum age.")

        # "Free" and a price are contradictory, and the card renders both.
        if cleaned.get("is_free") and (low or high):
            self.add_error(
                "is_free", "This is marked free but has a price. Clear one of them."
            )

        return cleaned


class ModeratorOccurrenceForm(OccurrenceForm):
    """A date row as a moderator sees it.

    One field more than the submitter's: cancelling a single date is a
    moderator's job and nobody else's. A submitter with a cancelled date would
    delete the row; a moderator must not, because "cancelled" is information a
    reader who already has the date in their calendar needs to see.
    """

    is_cancelled = forms.BooleanField(
        required=False,
        label="Cancelled",
        help_text="Keeps the date listed, struck through, rather than "
                  "removing it from a page someone may already have seen.",
    )


class BaseModeratorOccurrenceFormSet(BaseOccurrenceFormSet):
    """The dates of an existing event, edited by someone trusted with them.

    The submitter's formset refuses a set of dates that has entirely gone by,
    because the mistake it is aimed at is an extraction quietly dating an
    undated page to last year. That guard is wrong here: a moderator correcting
    the record of an event that already happened is doing something ordinary,
    and unlike a submitter reviewing a machine's guess they are looking
    straight at the year they typed.
    """

    reject_all_past = False
    row_fields = ("end", "note", "is_cancelled")


ModeratorOccurrenceFormSet = forms.formset_factory(
    ModeratorOccurrenceForm,
    formset=BaseModeratorOccurrenceFormSet,
    extra=EXTRA_OCCURRENCE_ROWS,
    max_num=MAX_OCCURRENCE_ROWS,
    can_delete=True,
    can_delete_extra=False,
)


class RefreshApplyForm(forms.Form):
    """Which of a re-read's proposed changes to take.

    Built from the diff at request time rather than declared, because the
    fields a page disagrees with are different every time. Validating against
    that list is what stops a stale form — one left open while a second
    refresh ran — writing a change the moderator never saw.
    """

    def __init__(self, changes, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.changes = changes
        for change in changes:
            self.fields[change.key] = forms.BooleanField(
                required=False, label=change.label, initial=True
            )

    def rows(self):
        """Each change beside its checkbox.

        Paired here rather than in the template because a template cannot look
        a bound field up by a variable name without a filter written for the
        purpose, and one method beats one filter.
        """
        return [(change, self[change.key]) for change in self.changes]

    def chosen(self):
        return [key for key, value in self.cleaned_data.items() if value]
