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


@pytest.mark.django_db
def test_signing_out_moved_off_the_nav_onto_the_profile(client, signed_in):
    """The top bar lost its Sign out; the profile page has to carry it.

    Removing one without adding the other leaves an account with no way out,
    which is the sort of thing nobody notices until they try.
    """
    nav = client.get(reverse("index")).content.decode()
    profile = client.get(reverse("profile")).content.decode()

    assert reverse("account_logout") not in nav
    assert reverse("account_logout") in profile


@pytest.mark.django_db
def test_signing_out_from_the_profile_works(client, signed_in):
    response = client.post(reverse("account_logout"), follow=True)
    assert not response.wsgi_request.user.is_authenticated


@pytest.fixture
def keyholder(django_user_model):
    """An ordinary account that has registered a passkey.

    Same shape as `reauthenticated` below, minus the sign-in — these tests are
    about what signing in *does*.
    """
    from allauth.account.models import EmailAddress
    from allauth.mfa.models import Authenticator

    user = django_user_model.objects.create_user(
        username="keyholder", email="keyholder@example.com", password="corn-flake-8842"
    )
    EmailAddress.objects.create(
        user=user, email=user.email, primary=True, verified=True
    )
    Authenticator.objects.create(
        user=user, type=Authenticator.Type.WEBAUTHN, data={}
    )
    return user


@pytest.mark.django_db
def test_a_passkey_does_not_turn_the_password_into_a_first_factor(client, keyholder):
    """Registering a passkey must not add a step to signing in with a password.

    allauth's mfa app reads any WebAuthn authenticator as a second factor, so
    out of the box this login stopped at "authenticate" and demanded the key as
    well. That is two-factor authentication arrived at by setting up Touch ID,
    on a site whose accounts post jumble sales — and with recovery codes
    deliberately out, it is a lockout waiting for the laptop holding the key to
    be somewhere else. `accounts.adapter.AccountAdapter` drops the stage; this
    is what notices if an allauth upgrade or a settings edit puts it back.
    """
    response = client.post(
        reverse("account_login"),
        {"login": keyholder.email, "password": "corn-flake-8842"},
        follow=True,
    )

    assert response.wsgi_request.user.is_authenticated
    assert reverse("mfa_authenticate") not in [url for url, _ in response.redirect_chain]


def test_no_second_factor_stage_is_configured():
    """The stage list itself, in case a login test ever passes for a lesser reason."""
    from allauth.account.adapter import get_adapter

    stages = get_adapter().get_login_stages()

    assert not [s for s in stages if s.startswith("allauth.mfa.stages.")]
    # The email-verification stage is not a factor and has to survive.
    assert "allauth.account.stages.EmailVerificationStage" in stages


@pytest.fixture
def reauthenticated(client, django_user_model):
    """Signed in with a password within allauth's reauthentication window.

    The passkey pages are behind a freshness check, and `force_login` does not
    record how you got in — so a forced session is redirected straight back out
    to reauthenticate, and any assertion on the form's markup passes against an
    empty body.
    """
    from allauth.account.models import EmailAddress

    user = django_user_model.objects.create_user(
        username="keyholder", email="keyholder@example.com", password="corn-flake-8842"
    )
    EmailAddress.objects.create(
        user=user, email=user.email, primary=True, verified=True
    )
    client.post(
        reverse("account_login"),
        {"login": user.email, "password": "corn-flake-8842"},
    )
    return user


@pytest.mark.django_db
def test_passkey_form_asks_only_for_a_name(client, reauthenticated):
    """The passwordless choice is made for the user, not by them.

    allauth offers a "Passwordless" checkbox explaining residentKey trade-offs,
    which is not a question to put to someone adding Touch ID. It has to stay
    in the DOM though — mfa/js/webauthn.js reads `.checked` off it, and without
    it every key registers as a second factor only.
    """
    body = client.get(reverse("mfa_add_webauthn")).content.decode()

    assert "Passwordless" not in body
    assert "biometrics or PIN protection" not in body
    assert 'id="id_passwordless"' in body, "the JS has nothing to read"
    assert "checked" in body, "passkeys would register as a second factor only"


@pytest.mark.django_db
def test_passkey_page_is_not_about_security_keys(client, reauthenticated):
    """"Add Security Key" describes a USB dongle nobody in a town owns."""
    body = client.get(reverse("mfa_add_webauthn")).content.decode()

    assert "Add a passkey" in body
    assert "Add Security Key" not in body


@pytest.mark.django_db
def test_no_authenticator_app_is_offered_anywhere(client, reauthenticated):
    """Passkeys are the whole of the offer here.

    A TOTP app is a second step on top of a password — a security ritual to ask
    of someone whose account can post a church bazaar. A passkey *replaces* the
    password and is less work than what it replaces. Offering both put an
    "Authenticator App / Activate" panel in front of someone who had come to
    set up Touch ID, which is how this was reported.
    """
    from django.urls import NoReverseMatch

    for page in (reverse("mfa_index"), reverse("profile")):
        body = client.get(page).content.decode()
        assert "Authenticator" not in body, f"{page} still offers an authenticator app"
        assert "Two-Factor" not in body, f"{page} frames passkeys as a second factor"

    with pytest.raises(NoReverseMatch):
        reverse("mfa_activate_totp")


@pytest.mark.django_db
def test_the_passkey_pages_say_passkey(client, reauthenticated):
    """allauth's own word throughout is "security key".

    Landing on "Two-Factor Authentication → Security Keys → You have added 1
    security key" after setting up Touch ID describes neither what was done nor
    what it is for. Every page reachable from the profile is overridden; this
    is what notices when an allauth upgrade adds one back.
    """
    for page in (reverse("mfa_index"), reverse("mfa_list_webauthn")):
        body = client.get(page).content.decode()
        assert "passkey" in body.lower(), f"{page} never says passkey"
        assert "security key" not in body.lower(), f"{page} still says security key"
