"""Form pieces that more than one audience needs to be identical.

A submitter confirming a draft and a moderator correcting a listing are
describing the same event, and for a long time they did it in two vocabularies:
the submitter typed a venue's name, address and town, while the moderator got a
`<select>` of venues that already existed. So a moderator could not create a
venue, could not fix a wrong address, and the one screen able to do either was
the Django admin — a staff account, which is a far larger grant than "may
correct a listing".

What lives here is only what both sides genuinely share: the input styling, and
the place fields with the rule for turning them back into records. The two form
classes stay separate, because what each audience may *change* is a real
difference — a submitter has no business setting prominence — and collapsing
them would only move that difference somewhere less visible.
"""

from django import forms

from events.services import organizer_for, set_venue

INPUT_CLASS = (
    "w-full rounded-lg border border-slate-300 px-3 py-2 text-sm "
    "focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500"
)


class PlaceFieldsMixin(forms.Form):
    """The venue and organizer as text, for anyone allowed to name one.

    Text rather than a foreign key on both sides. A dropdown cannot express
    "the hall you have is right but its address is wrong", and it cannot
    express a venue nobody has entered yet — which between them are most of
    the corrections anyone actually needs to make.

    Declared as a mixin over `forms.Form` so it composes with a plain form and
    a `ModelForm` alike; Django's metaclass collects the fields either way.
    """

    venue_name = forms.CharField(
        max_length=200, required=False, label="Venue",
        widget=forms.TextInput(attrs={"class": INPUT_CLASS}),
        help_text="Leave blank if the location isn't settled yet.",
    )
    venue_address = forms.CharField(
        max_length=300, required=False, label="Address",
        widget=forms.TextInput(attrs={"class": INPUT_CLASS}),
    )
    venue_city = forms.CharField(
        max_length=120, required=False, label="Town or city",
        widget=forms.TextInput(attrs={"class": INPUT_CLASS}),
    )
    organizer_name = forms.CharField(
        max_length=200, required=False, label="Organiser",
        widget=forms.TextInput(attrs={"class": INPUT_CLASS}),
    )

    def place_initial(self, event):
        """This event's venue and organizer as initial data for these fields."""
        venue = event.venue if event and event.venue_id else None
        organizer = event.organizer if event and event.organizer_id else None
        return {
            "venue_name": venue.name if venue else "",
            "venue_address": venue.address if venue else "",
            "venue_city": venue.city if venue else "",
            "organizer_name": organizer.name if organizer else "",
        }

    def apply_places(self, event):
        """Point `event` at the venue and organizer these fields name.

        Not saved here — the caller owns when the event is written, and a
        `ModelForm` has already decided that for itself.
        """
        event.venue = set_venue(
            event.venue if event.venue_id else None,
            self.cleaned_data.get("venue_name", ""),
            self.cleaned_data.get("venue_address", ""),
            self.cleaned_data.get("venue_city", ""),
        )
        event.organizer = organizer_for(self.cleaned_data.get("organizer_name", ""))
        return event
