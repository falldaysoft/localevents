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


# --- the query ladder ------------------------------------------------------
#
# Nominatim's free-text search is all-or-nothing: a venue name and a street
# address together can return nothing when either alone resolves. A live check
# against "Paris Fairgrounds, 139 Silver Street, Paris, ON" returned zero
# results while both halves returned one each, which is how a published event
# ended up with no marker on the map.


@pytest.mark.django_db
def test_the_ladder_drops_the_name_before_it_drops_the_address():
    """The full ladder, in order.

    Rung one keeps the name and the address together, because that is the most
    specific thing we know. After that a street address is a precise claim,
    while a name match is whatever OSM decided to call something nearby — so
    the name goes first.
    """
    venue = Venue.objects.create(
        name="Paris Fairgrounds", address="139 Silver Street", city="Paris, ON"
    )
    assert venue.geocode_queries() == [
        "Paris Fairgrounds, 139 Silver Street, Paris, ON",
        "139 Silver Street, Paris, ON",
        "Paris Fairgrounds, Paris, ON",
    ]


@pytest.mark.django_db
def test_a_venue_with_no_street_address_is_not_asked_twice():
    """Rungs one and three collapse to the same string; asking again would
    just spend another second of the rate limit."""
    venue = Venue.objects.create(name="Community Hall", city="Anytown")
    assert venue.geocode_queries() == ["Community Hall, Anytown"]


@pytest.mark.django_db
def test_a_venue_with_nothing_to_search_for_yields_no_queries():
    assert Venue.objects.create(name="   ").geocode_queries() == []


@pytest.mark.django_db
def test_geocoding_falls_back_when_the_full_query_finds_nothing(
    monkeypatch, settings
):
    """The Paris Fairgrounds case, end to end."""
    settings.MAP_BBOX = [43.0, -80.5, 43.3, -80.0]
    asked = []

    def fake_get(url, **kwargs):
        query = kwargs["params"]["q"]
        asked.append(query)
        if query == "Paris Fairgrounds, 139 Silver Street, Paris, ON":
            return FakeResponse([])
        return FakeResponse([{"lat": "43.2037783", "lon": "-80.4030700"}])

    monkeypatch.setattr(geocoding.httpx, "get", fake_get)
    monkeypatch.setattr(GeocodeThrottle, "acquire", classmethod(lambda cls, **kw: None))

    venue = Venue.objects.create(
        name="Paris Fairgrounds", address="139 Silver Street", city="Paris, ON"
    )
    geocoding.geocode_venue.call(venue.pk)

    venue.refresh_from_db()
    assert venue.geocode_status == Venue.GeocodeStatus.OK
    assert (venue.latitude, venue.longitude) == (43.2037783, -80.4030700)
    assert asked == [
        "Paris Fairgrounds, 139 Silver Street, Paris, ON",
        "139 Silver Street, Paris, ON",
    ]


@pytest.mark.django_db
def test_a_match_on_the_first_query_asks_only_once(monkeypatch, settings):
    """The fallback must not cost a second request in the normal case."""
    settings.MAP_BBOX = [43.0, -80.5, 43.3, -80.0]
    asked = []

    def fake_get(url, **kwargs):
        asked.append(kwargs["params"]["q"])
        return FakeResponse([{"lat": "43.1394", "lon": "-80.2644"}])

    monkeypatch.setattr(geocoding.httpx, "get", fake_get)
    monkeypatch.setattr(GeocodeThrottle, "acquire", classmethod(lambda cls, **kw: None))

    venue = Venue.objects.create(
        name="Community Hall", address="1 Main Street", city="Anytown"
    )
    geocoding.geocode_venue.call(venue.pk)

    assert len(asked) == 1


@pytest.mark.django_db
def test_a_transport_failure_stops_the_ladder_immediately(monkeypatch):
    """'Could not ask' is not 'no such place' — walking down the rungs would
    hammer a service that is already unhappy."""
    asked = []

    def explode(url, **kwargs):
        asked.append(kwargs["params"]["q"])
        raise httpx.ConnectError("down")

    monkeypatch.setattr(geocoding.httpx, "get", explode)
    monkeypatch.setattr(GeocodeThrottle, "acquire", classmethod(lambda cls, **kw: None))

    venue = Venue.objects.create(
        name="Paris Fairgrounds", address="139 Silver Street", city="Paris, ON"
    )
    geocoding.geocode_venue.call(venue.pk)

    venue.refresh_from_db()
    assert venue.geocode_status == Venue.GeocodeStatus.FAILED
    assert len(asked) == 1


@pytest.mark.django_db
def test_every_rung_missing_records_what_was_tried(monkeypatch):
    """"No match found" alone sent one investigation looking at the network."""
    monkeypatch.setattr(geocoding.httpx, "get", lambda url, **kw: FakeResponse([]))
    monkeypatch.setattr(GeocodeThrottle, "acquire", classmethod(lambda cls, **kw: None))

    venue = Venue.objects.create(
        name="Paris Fairgrounds", address="139 Silver Street", city="Paris, ON"
    )
    geocoding.geocode_venue.call(venue.pk)

    venue.refresh_from_db()
    assert venue.geocode_status == Venue.GeocodeStatus.FAILED
    assert "139 Silver Street, Paris, ON" in venue.geocode_error


# --- the region as a preference ---------------------------------------------
#
# Two live cases, both on one instance in the same week. "84 Highland Drive,
# Paris" resolved to a street of that name in Paris, Michigan, because the
# address-only rung matched there and the ladder stopped; "Watts Pond, Paris",
# the rung below, resolves to the right pond in the right Paris. And "Mount
# Pleasant, ON" resolved to a railway station in a city an hour away, with the
# village of that name inside the region third on the list. The region is what tells those apart, so it is
# sent as a Nominatim viewbox and an out-of-region hit keeps the ladder walking.


@pytest.mark.django_db
def test_lookup_sends_the_region_as_a_preference_not_a_fence(captured, settings):
    """`viewbox` ranks results inside the box first; `bounded` would drop the
    ones outside, and a hall just over the county line must still resolve."""
    settings.MAP_BBOX = [43.0, -80.5, 43.3, -80.0]
    geocoding.lookup("Mount Pleasant, ON", throttle=False)

    params = captured[0]["params"]
    assert params["viewbox"] == "-80.5,43.0,-80.0,43.3"  # lng,lat corners
    assert "bounded" not in params


@pytest.mark.django_db
def test_lookup_sends_no_viewbox_for_the_whole_world(captured, settings):
    settings.MAP_BBOX = [-90.0, -180.0, 90.0, 180.0]
    geocoding.lookup("Community Hall", throttle=False)

    assert "viewbox" not in captured[0]["params"]


MICHIGAN = {"lat": "43.7693829", "lon": "-85.5018784"}
IN_REGION = {"lat": "43.2173648", "lon": "-80.4044196"}


@pytest.mark.django_db
def test_an_out_of_region_match_does_not_end_the_ladder(monkeypatch, settings):
    """The Watts Pond case, end to end."""
    settings.MAP_BBOX = [42.95, -80.60, 43.35, -79.95]
    asked = []

    def fake_get(url, **kwargs):
        query = kwargs["params"]["q"]
        asked.append(query)
        return FakeResponse(
            {
                "Watts Pond, 84 Highland Drive, Paris": [],
                "84 Highland Drive, Paris": [MICHIGAN],
                "Watts Pond, Paris": [IN_REGION],
            }[query]
        )

    monkeypatch.setattr(geocoding.httpx, "get", fake_get)
    monkeypatch.setattr(GeocodeThrottle, "acquire", classmethod(lambda cls, **kw: None))

    venue = Venue.objects.create(
        name="Watts Pond", address="84 Highland Drive", city="Paris"
    )
    geocoding.geocode_venue.call(venue.pk)

    venue.refresh_from_db()
    assert venue.geocode_status == Venue.GeocodeStatus.OK
    assert (venue.latitude, venue.longitude) == (43.2173648, -80.4044196)
    assert len(asked) == 3


@pytest.mark.django_db
def test_an_out_of_region_match_is_kept_when_nothing_lower_lands_inside(
    monkeypatch, settings
):
    """Still a flag rather than a failure: the moderator sees *where* it landed."""
    settings.MAP_BBOX = [42.95, -80.60, 43.35, -79.95]

    def fake_get(url, **kwargs):
        query = kwargs["params"]["q"]
        return FakeResponse([MICHIGAN] if query == "84 Highland Drive, Paris" else [])

    monkeypatch.setattr(geocoding.httpx, "get", fake_get)
    monkeypatch.setattr(GeocodeThrottle, "acquire", classmethod(lambda cls, **kw: None))

    venue = Venue.objects.create(
        name="Watts Pond", address="84 Highland Drive", city="Paris"
    )
    geocoding.geocode_venue.call(venue.pk)

    venue.refresh_from_db()
    assert venue.geocode_status == Venue.GeocodeStatus.OUT_OF_REGION
    assert (venue.latitude, venue.longitude) == (43.7693829, -85.5018784)
    assert venue.geocode_error == ""


# --- asking again -------------------------------------------------------------


@pytest.mark.django_db
def test_the_admin_action_actually_enqueues(monkeypatch, rf):
    """It used to set the status to pending and stop, and nothing swept pending
    venues — so the one remedy for a wrong marker did nothing at all."""
    from django.contrib.admin.sites import AdminSite

    from events import admin as events_admin

    enqueued = []

    class StubTask:
        @staticmethod
        def enqueue(pk):
            enqueued.append(pk)

    monkeypatch.setattr(events_admin, "geocode_venue", StubTask)
    venue = Venue.objects.create(
        name="Watts Pond",
        city="Paris",
        latitude=43.7693829,
        longitude=-85.5018784,
        geocode_status=Venue.GeocodeStatus.OUT_OF_REGION,
    )
    model_admin = events_admin.VenueAdmin(Venue, AdminSite())
    monkeypatch.setattr(model_admin, "message_user", lambda *a, **kw: None)

    model_admin.mark_for_regeocoding(rf.post("/"), Venue.objects.filter(pk=venue.pk))

    venue.refresh_from_db()
    assert venue.geocode_status == Venue.GeocodeStatus.PENDING
    assert enqueued == [venue.pk]


@pytest.mark.django_db
def test_housekeeping_sweeps_pending_venues(monkeypatch):
    from django.core.management import call_command

    enqueued = []

    class StubTask:
        @staticmethod
        def enqueue(pk):
            enqueued.append(pk)

    monkeypatch.setattr(geocoding, "geocode_venue", StubTask)
    venue = Venue.objects.create(name="Community Hall", city="Anytown")
    venue.geocode_status = Venue.GeocodeStatus.PENDING
    venue.save(update_fields=["geocode_status"])

    call_command("run_housekeeping")

    assert venue.pk in enqueued
