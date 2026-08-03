"""Claiming a fresh instance from the browser.

The rule this file defends is narrow and load-bearing: `/claim/` exists only
while the site has no superuser, and the account it creates can sign in
immediately. Both halves matter. If the page outlives the first claim, anyone
can take an established site; if the account cannot sign in, the flow has not
actually replaced the two shell commands it was written to replace.
"""

import pytest
from django.urls import reverse

CLAIM = {
    "username": "founder",
    "email": "founder@example.com",
    "password1": "corn-flake-8842",
    "password2": "corn-flake-8842",
}


@pytest.fixture
def superuser(db, django_user_model):
    from allauth.account.models import EmailAddress
    from accounts.claim import reset_claim_cache

    user = django_user_model.objects.create_superuser(
        username="boss", email="boss@example.com", password="pw-12345678"
    )
    EmailAddress.objects.create(
        user=user, email=user.email, primary=True, verified=True
    )
    reset_claim_cache()
    return user


@pytest.mark.django_db
def test_unclaimed_site_offers_the_page(client):
    response = client.get(reverse("claim"))
    assert response.status_code == 200


@pytest.mark.django_db
def test_claiming_creates_a_superuser_who_is_signed_in(client, django_user_model):
    response = client.post(reverse("claim"), CLAIM)

    assert response.status_code == 302
    user = django_user_model.objects.get(email="founder@example.com")
    assert user.is_superuser and user.is_staff
    assert user.is_moderator
    assert response.wsgi_request.user == user


@pytest.mark.django_db
def test_the_claimed_account_can_sign_in_without_confirming_email(
    client, django_user_model
):
    """The point of the flow.

    `createsuperuser` left an account that mandatory verification locked out,
    which is why `verify_email` had to exist. Claiming must not reproduce that.
    """
    client.post(reverse("claim"), CLAIM)
    client.post(reverse("account_logout"))

    response = client.post(
        reverse("account_login"),
        {"login": "founder@example.com", "password": "corn-flake-8842"},
    )
    assert response.wsgi_request.user.is_authenticated


@pytest.mark.django_db
def test_page_is_gone_once_a_superuser_exists(client, superuser):
    assert client.get(reverse("claim")).status_code == 404


@pytest.mark.django_db
def test_posting_to_a_claimed_site_creates_nothing(client, superuser, django_user_model):
    response = client.post(reverse("claim"), CLAIM)

    assert response.status_code == 404
    assert not django_user_model.objects.filter(email="founder@example.com").exists()


@pytest.mark.django_db
def test_a_rival_claim_that_lands_first_wins(client, django_user_model, monkeypatch):
    """The check-then-act window between validating the form and saving it.

    A concurrent request, seen from inside this one, is a superuser appearing
    after the view's opening check has already passed. Hooking `SiteConfig
    .load` puts the rival at exactly that point: past validation, inside the
    transaction, immediately before the re-check that has to catch it.
    """
    from accounts import views

    original_load = views.SiteConfig.load

    def load_after_someone_else_won(*args, **kwargs):
        result = original_load(*args, **kwargs)
        django_user_model.objects.create_superuser(
            username="faster", email="faster@example.com", password="pw-12345678"
        )
        return result

    monkeypatch.setattr(views.SiteConfig, "load", load_after_someone_else_won)

    response = client.post(reverse("claim"), CLAIM)

    assert response.status_code == 404
    assert not django_user_model.objects.filter(email="founder@example.com").exists()


@pytest.mark.django_db
def test_weak_password_is_rejected(client, django_user_model):
    response = client.post(
        reverse("claim"),
        {**CLAIM, "password1": "password", "password2": "password"},
    )

    assert response.status_code == 400
    assert not django_user_model.objects.exists()


@pytest.mark.django_db
def test_mismatched_passwords_are_rejected(client, django_user_model):
    response = client.post(
        reverse("claim"),
        {**CLAIM, "password2": "corn-flake-8843"},
    )

    assert response.status_code == 400
    assert not django_user_model.objects.exists()


@pytest.mark.django_db
def test_banner_appears_only_while_unclaimed(client, superuser):
    from accounts.claim import reset_claim_cache

    claimed = client.get(reverse("index"))
    assert b"no administrator yet" not in claimed.content

    superuser.delete()
    reset_claim_cache()

    unclaimed = client.get(reverse("index"))
    assert b"no administrator yet" in unclaimed.content
