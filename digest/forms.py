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
