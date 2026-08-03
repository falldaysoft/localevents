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
