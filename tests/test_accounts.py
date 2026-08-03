"""Account rules that the product depends on.

Two things matter here and both are easy to regress silently: email is the
login identifier (username is only a display name), and confirmation is
mandatory — that requirement is most of what stands between the submission
queue and drive-by spam.
"""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse


@pytest.fixture
def confirmed_user(db, django_user_model):
    from allauth.account.models import EmailAddress

    user = django_user_model.objects.create_user(
        username="resident", email="resident@example.com", password="pw-12345678"
    )
    EmailAddress.objects.create(
        user=user, email=user.email, primary=True, verified=True
    )
    return user


@pytest.mark.django_db
def test_login_with_email_succeeds(client, confirmed_user):
    response = client.post(
        reverse("account_login"),
        {"login": "resident@example.com", "password": "pw-12345678"},
    )
    assert response.status_code == 302
    assert response.wsgi_request.user.is_authenticated


@pytest.mark.django_db
def test_login_with_username_is_rejected(client, confirmed_user):
    """Username is a display name, not a credential.

    The login field is type=email so a browser blocks this client-side, but
    that is not a security boundary — the server must reject it too.
    """
    response = client.post(
        reverse("account_login"),
        {"login": "resident", "password": "pw-12345678"},
    )
    assert response.status_code == 200  # redisplayed form, not a redirect
    assert not response.wsgi_request.user.is_authenticated


@pytest.mark.django_db
def test_unconfirmed_user_cannot_log_in(client, django_user_model):
    django_user_model.objects.create_user(
        username="nope", email="nope@example.com", password="pw-12345678"
    )
    response = client.post(
        reverse("account_login"),
        {"login": "nope@example.com", "password": "pw-12345678"},
    )
    assert not response.wsgi_request.user.is_authenticated


@pytest.mark.django_db
def test_signup_requires_both_username_and_email(client):
    response = client.post(
        reverse("account_signup"),
        {"email": "new@example.com", "password1": "pw-12345678", "password2": "pw-12345678"},
    )
    assert response.status_code == 200
    assert "username" in response.context["form"].errors


@pytest.mark.django_db
def test_verify_email_command_unblocks_a_shell_created_account(
    client, django_user_model
):
    """A createsuperuser account has no confirmation mail to click.

    Without this command the first deploy produces an admin who cannot sign in,
    which is exactly the sort of thing that is only discovered in production.
    """
    django_user_model.objects.create_superuser(
        username="admin", email="admin@example.com", password="pw-12345678"
    )

    before = client.post(
        reverse("account_login"),
        {"login": "admin@example.com", "password": "pw-12345678"},
    )
    assert not before.wsgi_request.user.is_authenticated

    call_command("verify_email", "admin@example.com")

    after = client.post(
        reverse("account_login"),
        {"login": "admin@example.com", "password": "pw-12345678"},
    )
    assert after.wsgi_request.user.is_authenticated


@pytest.mark.django_db
def test_verify_email_command_rejects_unknown_address():
    with pytest.raises(CommandError):
        call_command("verify_email", "ghost@example.com")


@pytest.mark.django_db
def test_is_moderator_requires_group_membership(django_user_model):
    from django.contrib.auth.models import Group

    user = django_user_model.objects.create_user(
        username="plain", email="plain@example.com", password="pw-12345678"
    )
    assert not user.is_moderator

    # The group is created by a data migration, so it is already there.
    user.groups.add(Group.objects.get(name="Moderators"))
    assert user.is_moderator


@pytest.mark.django_db
def test_superuser_is_always_a_moderator(django_user_model):
    root = django_user_model.objects.create_superuser(
        username="root", email="root@example.com", password="pw-12345678"
    )
    assert root.is_moderator


# --- the pages allauth renders for us --------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "/accounts/login/",
        "/accounts/signup/",
        "/accounts/password/reset/",
    ],
)
@pytest.mark.django_db
def test_auth_pages_are_inside_the_site(client, url):
    """allauth's pages must not look like a different website.

    Without templates/allauth/layouts/base.html, allauth silently falls back to
    its own bare layout: no Tailwind, no nav, no footer, and its own "Menu:"
    list. Nothing errors and no test failed — the sign-in page just looked
    unstyled for four phases before anyone opened it.
    """
    body = client.get(url).content.decode()

    assert "auth-page" in body, f"{url} is not using the site's allauth layout"
    assert "cdn.tailwindcss.com" in body, f"{url} has no stylesheet"
    assert "</footer>" in body, f"{url} is missing the site chrome"
    # allauth's own fallback nav, which the override replaces.
    assert "<strong>Menu:</strong>" not in body


@pytest.mark.parametrize(
    "url",
    ["/accounts/login/", "/accounts/signup/", "/accounts/password/reset/"],
)
@pytest.mark.django_db
def test_no_template_syntax_leaks_into_an_auth_page(client, url):
    """The same guard the public and moderation pages carry.

    Earned its place immediately: a multi-line {# #} comment added to base.html
    while fixing the layout put a literal block tag into the parsed template
    and 500'd every one of these pages.
    """
    body = client.get(url).content.decode()
    for marker in ("{#", "#}", "{% ", " %}"):
        assert marker not in body, f"raw template syntax {marker!r} in {url}"


@pytest.mark.django_db
def test_sign_in_page_offers_a_way_to_sign_up(client):
    """A button, not a sentence.

    allauth's stock login page opens with "If you have not created an account
    yet, then please sign up first" — a text link above the form that people
    do not see, which is exactly how it was reported. The site's only other
    route in is the nav's "Add an event", which is the right CTA and useless
    to someone hunting for the words "sign up".

    templates/account/login.html is a copy of allauth's, so an upgrade could
    quietly leave it behind. This is what notices.
    """
    body = client.get(reverse("account_login")).content.decode()

    assert reverse("account_signup") in body
    assert 'id="signup_link"' in body, "the signup call to action is not the button"
    assert "have not created an account yet" not in body, (
        "allauth's stock sentence is back — has the override been superseded?"
    )

