"""The three decisions, as forms.

Each one carries the text that reaches the submitter, so the labels say so
plainly. A moderator writing "no" into a box marked "internal note" and having
it emailed to a stranger is the kind of surprise worth designing out.
"""

from django import forms

from events.models import Category, Event

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
