"""Joining and leaving the weekly email (localevents.14.1).

What matters here: nothing changes on a GET, because mail scanners open every
link; the form never reveals whether an address already subscribes; an active
subscription cannot be hijacked by posting its address again; and the
one-click unsubscribe works with no cookie and no CSRF token, because that is
how a mail provider's button sends it.
"""

from datetime import timedelta

import pytest
from django.core import mail, signing
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from digest import services
from digest.models import Subscriber
from events import interest


@pytest.fixture(autouse=True)
def fresh_rate_limit():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def switched_on(db):
    from core.models import SiteConfig

    config = SiteConfig.load()
    config.digest_enabled = True
    config.save()


@pytest.fixture
def post_signup(client, django_capture_on_commit_callbacks):
    def post(email, using=None):
        with django_capture_on_commit_callbacks(execute=True):
            return (using or client).post(reverse("digest_signup"), {"email": email})

    return post


@pytest.fixture
def verified_user(django_user_model):
    from allauth.account.models import EmailAddress

    user = django_user_model.objects.create_user(
        username="resident", email="Resident@Example.com", password="pw-12345678"
    )
    EmailAddress.objects.create(
        user=user, email=user.email, primary=True, verified=True
    )
    return user


def confirm_url_from(message):
    line = next(l for l in message.body.splitlines() if "/digest/confirm/" in l)
    return line.strip().split("localhost:8000", 1)[-1]


# --- signing up ------------------------------------------------------------


@pytest.mark.django_db
def test_signup_creates_a_pending_row_and_mails_a_confirmation(post_signup):
    response = post_signup("  Reader@Example.com ")

    assert response.status_code == 302
    assert response.url == reverse("digest_sent")
    subscriber = Subscriber.objects.get()
    assert subscriber.email == "reader@example.com"
    assert subscriber.status == "pending"
    assert subscriber.source == Subscriber.Source.FORM

    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == ["reader@example.com"]
    assert message.reply_to  # a person, never the submissions inbox
    assert "/digest/confirm/" in message.body


@pytest.mark.django_db
def test_signup_hands_out_the_visitor_cookie_and_records_its_fingerprint(
    post_signup, client
):
    response = post_signup("reader@example.com")

    raw = response.cookies[interest.COOKIE_NAME].value
    vid = signing.loads(raw, salt=interest._SALT)
    assert Subscriber.objects.get().fingerprint == interest.fingerprint_for(vid)


@pytest.mark.django_db
def test_signup_keeps_an_existing_visitor_cookie(post_signup, client):
    client.cookies[interest.COOKIE_NAME] = signing.dumps("abc", salt=interest._SALT)

    response = post_signup("reader@example.com")

    assert interest.COOKIE_NAME not in response.cookies
    assert Subscriber.objects.get().fingerprint == interest.fingerprint_for("abc")


@pytest.mark.django_db
def test_an_active_address_gets_the_same_answer_and_no_mail(post_signup):
    Subscriber.objects.create(
        email="reader@example.com", source="form", fingerprint="theirs",
        confirmed_at=timezone.now(),
    )

    response = post_signup("reader@example.com")

    assert response.status_code == 302
    assert response.url == reverse("digest_sent")
    assert mail.outbox == []
    # Posting someone's address must not attach the poster's marks to it.
    assert Subscriber.objects.get().fingerprint == "theirs"


@pytest.mark.django_db
def test_repeated_signups_send_one_confirmation_per_window(post_signup):
    post_signup("reader@example.com")
    post_signup("reader@example.com")
    post_signup("reader@example.com")
    assert len(mail.outbox) == 1

    Subscriber.objects.update(
        confirmation_sent_at=timezone.now() - services.RESEND_AFTER - timedelta(seconds=1)
    )
    post_signup("reader@example.com")
    assert len(mail.outbox) == 2


@pytest.mark.django_db
def test_an_invalid_address_is_a_400_with_the_form(client):
    response = client.post(reverse("digest_signup"), {"email": "not-an-address"})
    assert response.status_code == 400
    assert not Subscriber.objects.exists()


@pytest.mark.django_db
def test_signups_are_rate_limited_per_address(post_signup, settings):
    settings.DIGEST_SIGNUPS_PER_HOUR = 2
    post_signup("a@example.com")
    post_signup("b@example.com")
    response = post_signup("c@example.com")
    assert response.status_code == 429
    assert Subscriber.objects.count() == 2


@pytest.mark.django_db
def test_the_signup_page_and_footer_link(client):
    response = client.get(reverse("digest_signup"))
    assert response.status_code == 200
    assert b'name="email"' in response.content
    home = client.get(reverse("index"))
    assert reverse("digest_signup").encode() in home.content


# --- confirming ------------------------------------------------------------


@pytest.mark.django_db
def test_opening_the_confirmation_link_changes_nothing(post_signup, client):
    post_signup("reader@example.com")
    url = confirm_url_from(mail.outbox[0])

    response = client.get(url)

    assert response.status_code == 200
    assert b"Yes, send it to me" in response.content
    assert Subscriber.objects.get().status == "pending"


@pytest.mark.django_db
def test_pressing_the_button_confirms(post_signup, client):
    post_signup("reader@example.com")
    url = confirm_url_from(mail.outbox[0])

    response = client.post(url)

    assert response.status_code == 200
    subscriber = Subscriber.objects.get()
    assert subscriber.is_active
    assert "Confirmed by the link" in subscriber.consent_note


@pytest.mark.django_db
def test_a_tampered_or_expired_link_is_refused(post_signup, client, settings):
    post_signup("reader@example.com")
    url = confirm_url_from(mail.outbox[0])

    assert client.post(url[:-3] + "xyz/").status_code == 404

    later = timezone.now() + services.CONFIRM_MAX_AGE + timedelta(minutes=1)
    import time

    real = time.time
    try:
        time.time = lambda: later.timestamp()
        assert client.post(url).status_code == 404
    finally:
        time.time = real
    assert Subscriber.objects.get().status == "pending"


@pytest.mark.django_db
def test_a_link_for_an_old_address_cannot_confirm_a_changed_row(client):
    subscriber = Subscriber.objects.create(email="old@example.com", source="form")
    key = services.confirmation_key(subscriber)
    Subscriber.objects.filter(pk=subscriber.pk).update(email="new@example.com")

    assert client.post(reverse("digest_confirm", args=[key])).status_code == 404


# --- unsubscribing ---------------------------------------------------------


@pytest.fixture
def active(db):
    return Subscriber.objects.create(
        email="reader@example.com", source="form", confirmed_at=timezone.now()
    )


@pytest.mark.django_db
def test_opening_the_unsubscribe_link_changes_nothing(client, active):
    response = client.get(reverse("digest_unsubscribe", args=[active.token]))
    assert response.status_code == 200
    active.refresh_from_db()
    assert active.is_active


@pytest.mark.django_db
def test_one_click_unsubscribe_needs_no_cookie_or_csrf_token(active):
    client = Client(enforce_csrf_checks=True)

    response = client.post(
        reverse("digest_unsubscribe", args=[active.token]),
        {"List-Unsubscribe": "One-Click"},
    )

    assert response.status_code == 200
    active.refresh_from_db()
    assert active.status == "unsubscribed"


@pytest.mark.django_db
def test_an_unknown_unsubscribe_token_is_a_404(client):
    assert client.post(reverse("digest_unsubscribe", args=["nope"])).status_code == 404


@pytest.mark.django_db
def test_signing_up_again_after_leaving_needs_a_fresh_confirmation(
    post_signup, client, active
):
    active.deactivate()

    post_signup("reader@example.com")

    active.refresh_from_db()
    assert not active.is_active
    assert len(mail.outbox) == 1
    client.post(confirm_url_from(mail.outbox[0]))
    active.refresh_from_db()
    assert active.is_active


# --- signed-in readers -----------------------------------------------------


@pytest.mark.django_db
def test_a_verified_reader_typing_their_own_address_is_subscribed_at_once(
    post_signup, client, verified_user
):
    client.force_login(verified_user)

    post_signup("resident@example.com")

    subscriber = Subscriber.objects.get()
    assert subscriber.is_active
    assert subscriber.user == verified_user
    assert mail.outbox == []


@pytest.mark.django_db
def test_a_signed_in_reader_typing_another_address_still_confirms_it(
    post_signup, client, verified_user
):
    client.force_login(verified_user)

    post_signup("someone.else@example.com")

    assert Subscriber.objects.get().status == "pending"
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_the_profile_switch_turns_it_on_and_off(client, verified_user):
    client.force_login(verified_user)
    assert b"Turn on" in client.get(reverse("profile")).content

    client.post(reverse("digest_profile_toggle"), {"on": "1"})
    subscriber = Subscriber.objects.get()
    assert subscriber.is_active
    assert subscriber.source == Subscriber.Source.PROFILE
    assert subscriber.user == verified_user
    assert b"Turn off" in client.get(reverse("profile")).content

    client.post(reverse("digest_profile_toggle"), {"on": "0"})
    subscriber.refresh_from_db()
    assert subscriber.status == "unsubscribed"


@pytest.mark.django_db
def test_an_unverified_account_cannot_turn_it_on(client, django_user_model):
    user = django_user_model.objects.create_user(
        username="new", email="new@example.com", password="pw-12345678"
    )
    client.force_login(user)

    client.post(reverse("digest_profile_toggle"), {"on": "1"})

    assert not Subscriber.objects.exists()


@pytest.mark.django_db
def test_signing_in_links_an_anonymous_subscription_to_the_account(
    client, verified_user
):
    subscriber = Subscriber.objects.create(
        email="resident@example.com", source="form", confirmed_at=timezone.now()
    )

    client.force_login(verified_user)  # sends user_logged_in, as every way in does

    subscriber.refresh_from_db()
    assert subscriber.user == verified_user


@pytest.mark.django_db
def test_signing_in_does_not_take_someone_elses_subscription(client, verified_user):
    Subscriber.objects.create(email="other@example.com", source="form")

    client.force_login(verified_user)
    services.link_user(verified_user)

    assert Subscriber.objects.get().user is None
