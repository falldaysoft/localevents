from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import (
    get_password_validators,
    validate_password,
)
from django.db import transaction

# This form is the one place a superuser is minted from a public page, so it
# carries its own password policy instead of inheriting the project's.
# AUTH_PASSWORD_VALIDATORS is deliberately unset — a resident posting a church
# bazaar should not be lectured about entropy — but the account that can edit
# every listing on the site is a different proposition.
CLAIM_PASSWORD_VALIDATORS = get_password_validators(
    [
        {
            "NAME": "django.contrib.auth.password_validation."
            "UserAttributeSimilarityValidator"
        },
        {
            "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
            "OPTIONS": {"min_length": 12},
        },
        {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
        {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
    ]
)


class ClaimForm(forms.Form):
    """Create the first administrator of an unclaimed site.

    Deliberately not allauth's SignupForm. That one mails a confirmation link
    and leaves the account unusable until it is clicked, which is wrong for
    exactly one account: the first administrator is the person who configures
    the SMTP relay, so requiring working email to create them is circular.
    This marks the address verified outright instead — safe precisely because
    the form runs once, on a site with no other users, and the claimant proves
    control of nothing because there is nothing yet to control.
    """

    username = forms.CharField(
        max_length=150,
        label="Display name",
        help_text="Shown against events you post. Not used to sign in.",
    )
    email = forms.EmailField(
        label="Email address",
        help_text="This is what you will sign in with.",
    )
    password1 = forms.CharField(
        label="Password",
        widget=forms.PasswordInput,
        help_text="At least 12 characters.",
    )
    password2 = forms.CharField(label="Confirm password", widget=forms.PasswordInput)

    def clean_username(self):
        username = self.cleaned_data["username"]
        if get_user_model().objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("That display name is already taken.")
        return username

    def clean_email(self):
        email = self.cleaned_data["email"]
        if get_user_model().objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with that email already exists.")
        return email

    def clean(self):
        cleaned = super().clean()
        password1 = cleaned.get("password1")
        password2 = cleaned.get("password2")

        if password1 and password2 and password1 != password2:
            self.add_error("password2", "The two passwords do not match.")
            return cleaned

        if password1:
            # Run the configured validators against an unsaved instance, so the
            # similarity check can see the name and address being claimed.
            candidate = get_user_model()(
                username=cleaned.get("username") or "",
                email=cleaned.get("email") or "",
            )
            try:
                validate_password(
                    password1,
                    candidate,
                    password_validators=CLAIM_PASSWORD_VALIDATORS,
                )
            except forms.ValidationError as exc:
                self.add_error("password1", exc)

        return cleaned

    def save(self):
        from allauth.account.models import EmailAddress

        with transaction.atomic():
            user = get_user_model().objects.create_user(
                username=self.cleaned_data["username"],
                email=self.cleaned_data["email"],
                password=self.cleaned_data["password1"],
                is_staff=True,
                is_superuser=True,
            )
            EmailAddress.objects.create(
                user=user, email=user.email, primary=True, verified=True
            )
        return user
