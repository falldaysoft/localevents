"""Geocoding, including the part that keeps us welcome at Nominatim.

Their policy is one request per second and a contactable User-Agent. Both are
tested here, because breaking either gets an IP blocked and the failure would
otherwise only show up in production.
"""

import json

import httpx
import pytest

from events import geocoding
from events.models import GeocodeThrottle, Venue


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom", request=None, response=None
            )

    def json(self):
        if isinstance(self._payload, str):
            return json.loads(self._payload)
        return self._payload


@pytest.fixture
def captured(monkeypatch):
    """Capture the outbound request instead of making one."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return FakeResponse([{"lat": "43.1394", "lon": "-80.2644"}])

    monkeypatch.setattr(geocoding.httpx, "get", fake_get)
    return calls


@pytest.mark.django_db
def test_lookup_sends_a_contactable_user_agent(captured, settings):
    """Nominatim blocks requests without one; this is not decoration."""
    settings.USER_AGENT = "Test Events bot (hello@example.org)"
    geocoding.lookup("Community Hall", throttle=False)

    assert captured[0]["headers"]["User-Agent"] == "Test Events bot (hello@example.org)"


@pytest.mark.django_db
def test_lookup_returns_coordinates(captured):
    assert geocoding.lookup("Community Hall", throttle=False) == (43.1394, -80.2644)


@pytest.mark.django_db
def test_lookup_returns_none_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(
        geocoding.httpx, "get", lambda url, **kw: FakeResponse([])
    )
    assert geocoding.lookup("nowhere at all", throttle=False) is None


@pytest.mark.django_db
def test_lookup_raises_on_transport_failure(monkeypatch):
    def explode(url, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(geocoding.httpx, "get", explode)
    with pytest.raises(geocoding.GeocodeError):
        geocoding.lookup("Community Hall", throttle=False)


@pytest.mark.django_db
def test_lookup_takes_the_throttle_before_calling(monkeypatch):
    """The rate limit must be claimed on the request path, not left to callers."""
    order = []

    monkeypatch.setattr(
        GeocodeThrottle, "acquire", classmethod(lambda cls, **kw: order.append("throttle"))
    )
    monkeypatch.setattr(
        geocoding.httpx,
        "get",
        lambda url, **kw: (order.append("http"), FakeResponse([]))[1],
    )

    geocoding.lookup("Community Hall")
    assert order == ["throttle", "http"]


@pytest.mark.django_db
def test_geocode_venue_stores_coordinates_and_marks_ok(captured, settings):
    settings.MAP_BBOX = [43.0, -80.5, 43.3, -80.0]
    venue = Venue.objects.create(name="Community Hall", city="Anytown")

    geocoding.geocode_venue.call(venue.pk)

    venue.refresh_from_db()
    assert venue.has_coordinates
    assert venue.geocode_status == Venue.GeocodeStatus.OK
    assert venue.geocode_error == ""


@pytest.mark.django_db
def test_geocode_venue_flags_a_result_outside_the_region(captured, settings):
    """Outside the bounds is recorded, not discarded.

    The coordinates are still stored so a moderator can see *where* it landed
    and decide, rather than being told only that something went wrong.
    """
    settings.MAP_BBOX = [51.0, -1.0, 52.0, 1.0]  # somewhere else entirely
    venue = Venue.objects.create(name="Community Hall", city="Anytown")

    geocoding.geocode_venue.call(venue.pk)

    venue.refresh_from_db()
    assert venue.geocode_status == Venue.GeocodeStatus.OUT_OF_REGION
    assert venue.has_coordinates


@pytest.mark.django_db
def test_geocode_venue_records_failure_without_raising(monkeypatch):
    """A venue that cannot be found must not be retried forever by the queue."""
    def explode(url, **kwargs):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(geocoding.httpx, "get", explode)
    venue = Venue.objects.create(name="Nowhere", city="Anytown")

    geocoding.geocode_venue.call(venue.pk)  # must not raise

    venue.refresh_from_db()
    assert venue.geocode_status == Venue.GeocodeStatus.FAILED
    assert venue.geocode_error


@pytest.mark.django_db
def test_geocode_venue_leaves_hand_set_coordinates_alone(captured):
    """A moderator's correction outranks the geocoder."""
    venue = Venue.objects.create(
        name="Hall",
        latitude=1.0,
        longitude=2.0,
        geocode_status=Venue.GeocodeStatus.MANUAL,
    )

    geocoding.geocode_venue.call(venue.pk)

    venue.refresh_from_db()
    assert (venue.latitude, venue.longitude) == (1.0, 2.0)
    assert captured == []


@pytest.mark.django_db
def test_geocode_venue_handles_a_deleted_venue(captured):
    geocoding.geocode_venue.call(999999)  # must not raise


@pytest.mark.django_db
def test_geocode_venue_fails_cleanly_with_nothing_to_search_for(captured):
    venue = Venue.objects.create(name="   ")
    geocoding.geocode_venue.call(venue.pk)
    venue.refresh_from_db()
    assert venue.geocode_status == Venue.GeocodeStatus.FAILED
    assert captured == []


@pytest.mark.django_db
def test_pending_sweep_is_bounded(monkeypatch):
    """One run must not monopolise the worker at a request per second."""
    enqueued = []

    # Task objects are frozen dataclasses, so swap the module-level name rather
    # than trying to patch an attribute on the task itself.
    class StubTask:
        @staticmethod
        def enqueue(pk):
            enqueued.append(pk)

    monkeypatch.setattr(geocoding, "geocode_venue", StubTask)
    for i in range(10):
        Venue.objects.create(name=f"Venue {i}", city="Anytown")

    geocoding.geocode_pending_venues.call(limit=3)
    assert len(enqueued) == 3
