"""The reader's "Interested" mark, and the page that remembers it.

What matters here: an anonymous reader is remembered by a cookie the first
click hands out, a signed-in reader by their account, and signing in folds the
former into the latter. The bot protection is a POST behind CSRF plus a
per-address cap, and both are held to exactly that.
"""

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from events import interest
from events.models import Event, Interest, Occurrence, Venue
from submissions.models import Submission

HX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture(autouse=True)
def fresh_rate_limit():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def event(db):
    hall = Venue.objects.create(name="Community Hall", city="Anytown")
    event = Event.objects.create(
        title="Open Mic Night", venue=hall, status=Event.Status.PUBLISHED,
        prominence=Event.Prominence.LISTED,
    )
    Occurrence.objects.create(event=event, start=timezone.now() + timedelta(days=2))
    return event


@pytest.fixture
def past_event(db):
    event = Event.objects.create(
        title="Last Month's Fair", status=Event.Status.PUBLISHED,
        prominence=Event.Prominence.LISTED,
    )
    Occurrence.objects.create(event=event, start=timezone.now() - timedelta(days=30))
    return event


@pytest.fixture
def confirmed_user(django_user_model):
    from allauth.account.models import EmailAddress

    user = django_user_model.objects.create_user(
        username="resident", email="resident@example.com", password="pw-12345678"
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    return user


def toggle(client, event, **extra):
    return client.post(reverse("event_interest", args=[event.slug]), **extra)


# --- anonymous ---------------------------------------------------------------


def test_first_click_hands_out_a_cookie_and_records_the_mark(client, event):
    response = toggle(client, event, **HX)

    assert response.status_code == 200
    assert interest.COOKIE_NAME in response.cookies
    cookie = response.cookies[interest.COOKIE_NAME]
    assert cookie["httponly"]
    assert cookie["samesite"] == "Lax"
    assert int(cookie["max-age"]) >= 60 * 60 * 24 * 365

    event.refresh_from_db()
    assert event.interest_count == 1
    row = Interest.objects.get()
    assert row.user is None
    # The stored fingerprint is not the cookie's value: a database dump must
    # hand out no valid cookies.
    assert row.fingerprint and row.fingerprint not in cookie.value
    assert 'aria-pressed="true"' in response.content.decode()


def test_second_click_with_the_cookie_withdraws_it(client, event):
    toggle(client, event, **HX)
    response = toggle(client, event, **HX)

    event.refresh_from_db()
    assert event.interest_count == 0
    assert not Interest.objects.exists()
    assert 'aria-pressed="false"' in response.content.decode()
    # An existing cookie is not re-issued.
    assert interest.COOKIE_NAME not in response.cookies


def test_a_tampered_cookie_is_a_fresh_identity_not_an_error(client, event):
    toggle(client, event, **HX)
    client.cookies[interest.COOKIE_NAME] = "forged:value"

    response = toggle(client, event, **HX)

    assert response.status_code == 200
    assert interest.COOKIE_NAME in response.cookies
    # Two visitors now, as far as the site can tell.
    assert Interest.objects.count() == 2


def test_the_site_remembers_the_mark_on_the_next_visit(client, event):
    toggle(client, event, **HX)

    body = client.get(reverse("event_detail", args=[event.slug])).content.decode()
    assert 'aria-pressed="true"' in body
    front = client.get(reverse("index")).content.decode()
    assert 'aria-pressed="true"' in front


def test_a_stranger_sees_the_mark_count_but_not_the_pressed_state(client, event):
    toggle(client, event, **HX)
    client.cookies.clear()

    body = client.get(reverse("event_detail", args=[event.slug])).content.decode()
    assert 'aria-pressed="false"' in body
    assert 'aria-pressed="true"' not in body


# --- signed in ----------------------------------------------------------------


def test_a_signed_in_reader_is_their_account_not_a_cookie(client, event, confirmed_user):
    client.force_login(confirmed_user)
    response = toggle(client, event, **HX)

    assert response.status_code == 200
    assert interest.COOKIE_NAME not in response.cookies
    assert Interest.objects.get().user == confirmed_user

    toggle(client, event, **HX)
    assert not Interest.objects.exists()


def test_signing_in_folds_the_devices_marks_into_the_account(
    client, event, past_event, confirmed_user
):
    """The page said it would remember; an account should not forget it."""
    toggle(client, event, **HX)
    toggle(client, past_event, **HX)
    # The account already marked one of them from another device.
    Interest.objects.create(event=past_event, user=confirmed_user)
    assert Event.objects.get(pk=past_event.pk).interest_count == 2

    response = client.post(
        reverse("account_login"),
        {"login": "resident@example.com", "password": "pw-12345678"},
    )
    assert response.status_code == 302

    rows = Interest.objects.filter(user=confirmed_user)
    assert set(rows.values_list("event_id", flat=True)) == {event.pk, past_event.pk}
    assert not Interest.objects.filter(user__isnull=True).exists()
    # One person, one mark: the duplicate came off the count.
    assert Event.objects.get(pk=past_event.pk).interest_count == 1
    assert Event.objects.get(pk=event.pk).interest_count == 1


def test_signing_in_with_nothing_marked_changes_nothing(client, confirmed_user):
    response = client.post(
        reverse("account_login"),
        {"login": "resident@example.com", "password": "pw-12345678"},
    )
    assert response.status_code == 302
    assert not Interest.objects.exists()


# --- the gate -----------------------------------------------------------------


def test_a_crawler_cannot_mark_interest_by_following_a_link(client, event):
    response = client.get(reverse("event_interest", args=[event.slug]))
    assert response.status_code == 405
    assert not Interest.objects.exists()


def test_the_toggle_is_behind_csrf(event):
    from django.test import Client

    strict = Client(enforce_csrf_checks=True)
    response = strict.post(reverse("event_interest", args=[event.slug]), **HX)
    assert response.status_code == 403
    assert not Interest.objects.exists()


def test_only_published_events_take_a_mark(client, event):
    event.status = Event.Status.UNPUBLISHED
    event.save()
    assert toggle(client, event, **HX).status_code == 404


def test_one_address_is_capped_per_hour(client, event, settings):
    settings.INTEREST_TOGGLES_PER_HOUR = 3
    for _ in range(3):
        assert toggle(client, event, **HX).status_code == 200

    response = toggle(client, event, **HX)
    assert response.status_code == 429
    # Capped, not confused: the fourth click changed nothing.
    assert Event.objects.get(pk=event.pk).interest_count == 1

    # Another address is another budget.
    response = toggle(client, event, HTTP_X_FORWARDED_FOR="203.0.113.9", **HX)
    assert response.status_code == 200


def test_the_rate_limit_keys_on_the_proxys_view_of_the_client():
    from django.test import RequestFactory

    request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="10.0.0.1, 203.0.113.9")
    assert interest.client_ip(request) == "203.0.113.9"
    request = RequestFactory().get("/")
    assert interest.client_ip(request) == "127.0.0.1"


# --- the fallback and the button ------------------------------------------------


def test_without_htmx_the_reader_is_sent_back_where_they_were(client, event):
    response = toggle(client, event, data={"next": "/?when=weekend"})
    assert response.status_code == 302
    assert response["Location"] == "/?when=weekend"
    assert Interest.objects.count() == 1


def test_an_offsite_next_is_ignored(client, event):
    response = toggle(client, event, data={"next": "https://evil.example/"})
    assert response.status_code == 302
    assert response["Location"] == reverse("event_detail", args=[event.slug])


def test_the_button_renders_without_alpine(client, event):
    body = client.get(reverse("event_detail", args=[event.slug])).content.decode()
    assert "x-data" not in body and "x-show" not in body
    assert "{%" not in body and "{{" not in body


# --- My events -----------------------------------------------------------------


def test_my_events_lists_what_this_device_marked(client, event, past_event):
    other = Event.objects.create(
        title="Someone Else's Thing", status=Event.Status.PUBLISHED,
        prominence=Event.Prominence.LISTED,
    )
    Occurrence.objects.create(event=other, start=timezone.now() + timedelta(days=1))

    toggle(client, event, **HX)
    toggle(client, past_event, **HX)
    body = client.get(reverse("my_events")).content.decode()

    assert "Open Mic Night" in body
    assert "Last Month" in body  # under "Already happened"
    assert "Someone Else" not in body
    assert "Already happened" in body
    # Not signed in: no submissions half, and a nudge to sign in.
    assert 'id="submitted"' not in body
    assert "remembered on this device" in body
    assert 'name="robots" content="noindex"' in body


def test_my_events_is_empty_but_welcoming_for_a_new_visitor(client, db):
    body = client.get(reverse("my_events")).content.decode()
    assert "on any event to keep it here" in body


def test_my_events_pairs_marks_with_submissions_when_signed_in(client, event, confirmed_user):
    client.force_login(confirmed_user)
    toggle(client, event, **HX)
    Submission.objects.create(
        submitted_by=confirmed_user, status=Submission.Status.PENDING_REVIEW,
        draft={"title": "Riverside Barbecue"},
    )

    body = client.get(reverse("my_events")).content.decode()
    assert "Open Mic Night" in body
    assert 'id="submitted"' in body
    assert "Riverside Barbecue" in body
    assert "remembered on this device" not in body


def test_my_events_is_offered_once_there_is_something_to_see(client, event):
    front = client.get(reverse("index")).content.decode()
    assert reverse("my_events") not in front

    toggle(client, event, **HX)
    front = client.get(reverse("index")).content.decode()
    assert reverse("my_events") in front


def test_the_old_submissions_url_lands_on_my_events(client, confirmed_user):
    client.force_login(confirmed_user)
    response = client.get(reverse("my_submissions"))
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("my_events"))


# --- the service ---------------------------------------------------------------


def test_the_moderators_star_is_the_same_mark(event, confirmed_user):
    from moderation.services import toggle_interest

    assert toggle_interest(event, confirmed_user) is True
    assert Interest.objects.get().user == confirmed_user
    assert toggle_interest(event, confirmed_user) is False
    assert not Interest.objects.exists()


def test_the_service_insists_on_exactly_one_identity(event, confirmed_user):
    from events.services import toggle_interest

    with pytest.raises(ValueError):
        toggle_interest(event)
    with pytest.raises(ValueError):
        toggle_interest(event, user=confirmed_user, fingerprint="abc")
