"""The account page behind your name in the nav.

Mostly a hub, so most of what matters is that it is reachable and that its
links point at pages that exist. The one thing it owns is the display name,
which is public — it appears against every event you post — so editing it is
worth a test of its own.
"""

import pytest
from django.urls import reverse


@pytest.fixture
def signed_in(client, submitter):
    client.force_login(submitter)
    return submitter


@pytest.mark.django_db
def test_profile_requires_signing_in(client):
    response = client.get(reverse("profile"))
    assert response.status_code == 302
    assert reverse("account_login") in response.url


@pytest.mark.django_db
def test_profile_links_to_passkey_enrolment(client, signed_in):
    """The reason this page exists.

    Nothing in the site linked to allauth's MFA pages, so passkeys were only
    reachable by typing the URL.
    """
    response = client.get(reverse("profile"))

    assert response.status_code == 200
    assert reverse("mfa_add_webauthn").encode() in response.content
    assert reverse("account_change_password").encode() in response.content
    assert reverse("account_email").encode() in response.content


@pytest.mark.django_db
def test_name_in_the_nav_links_to_the_profile(client, signed_in):
    response = client.get(reverse("index"))
    assert f'href="{reverse("profile")}"'.encode() in response.content


@pytest.mark.django_db
def test_display_name_can_be_changed(client, signed_in):
    response = client.post(reverse("profile"), {"username": "newname"})

    assert response.status_code == 302
    signed_in.refresh_from_db()
    assert signed_in.username == "newname"


@pytest.mark.django_db
def test_display_name_cannot_collide(client, signed_in, moderator):
    response = client.post(reverse("profile"), {"username": moderator.username})

    assert response.status_code == 400
    signed_in.refresh_from_db()
    assert signed_in.username == "submitter"


@pytest.mark.django_db
def test_moderator_links_are_hidden_from_ordinary_members(client, signed_in):
    response = client.get(reverse("profile"))
    assert reverse("mod_queue").encode() not in response.content


@pytest.mark.django_db
def test_moderator_sees_the_queue_link(client, moderator):
    client.force_login(moderator)
    response = client.get(reverse("profile"))
    assert reverse("mod_queue").encode() in response.content


@pytest.mark.django_db
def test_registered_passkey_switches_the_link_to_manage(client, signed_in):
    from allauth.mfa.models import Authenticator

    Authenticator.objects.create(
        user=signed_in, type=Authenticator.Type.WEBAUTHN, data={}
    )

    response = client.get(reverse("profile"))
    assert reverse("mfa_list_webauthn").encode() in response.content
    assert b"1 registered" in response.content
