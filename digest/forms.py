from django import forms


class SignupForm(forms.Form):
    email = forms.EmailField(
        label="Email address",
        widget=forms.EmailInput(
            attrs={
                "autocomplete": "email",
                "placeholder": "you@example.com",
                "class": "w-full rounded-lg border border-slate-300 px-3 py-2",
            }
        ),
    )


_INPUT = "w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"


class NoteForm(forms.Form):
    """A moderator's change to someone's subscription, and how they asked.

    The note is required because it is the consent record: "asked at the
    library stall, 12 Nov" is what answers a complaint months later.
    """

    note = forms.CharField(
        max_length=200,
        label="How did they ask?",
        widget=forms.TextInput(
            attrs={"class": _INPUT, "placeholder": "e.g. asked by phone, 12 Nov"}
        ),
    )


class ModeratorSubscribeForm(NoteForm):
    email = forms.EmailField(
        label="Email address",
        widget=forms.EmailInput(attrs={"class": _INPUT, "autocomplete": "off"}),
    )
    field_order = ["email", "note"]
