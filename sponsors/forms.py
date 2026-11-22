from django import forms

INPUT = "w-full rounded-lg border border-slate-300 px-3 py-2"


class PromoteForm(forms.Form):
    starts_on = forms.DateField(
        label="Start on",
        widget=forms.DateInput(attrs={"type": "date", "class": INPUT}),
        help_text="It starts on this day, or when an editor approves it if "
        "that is later.",
    )
    note = forms.CharField(
        label="A note for the editor (optional)",
        required=False,
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3, "class": INPUT}),
    )
    agree = forms.BooleanField(
        label="I understand an editor reviews every sponsorship, and my card "
        "is charged only if it is approved.",
    )


class DecisionForm(forms.Form):
    note = forms.CharField(
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3, "class": INPUT}),
    )
