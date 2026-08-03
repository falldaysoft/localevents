"""The public browse experience.

Covers the two things a visitor actually relies on: that filters narrow what
they see and stay in the URL, and that a weekly thing appears once rather than
once per week.
"""

import json
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from events.models import Category, Event, Occurrence, Organizer, Venue
from web.filters import EventFilter


@pytest.fixture
def world(db):
    now = timezone.now()
    hall = Venue.objects.create(
        name="Community Hall", city="Anytown",
        latitude=43.1, longitude=-80.2,
        geocode_status=Venue.GeocodeStatus.OK,
    )
    studio = Venue.objects.create(
        name="Craft Studio", city="Anytown",
        latitude=43.11, longitude=-80.21,
        geocode_status=Venue.GeocodeStatus.OK,
    )
    music = Category.objects.get(slug="music")
    learning = Category.objects.get(slug="learning")

    gig = Event.objects.create(
        title="Open Mic Night", venue=hall, is_free=True,
        status=Event.Status.PUBLISHED, prominence=Event.Prominence.LISTED,
    )
    gig.categories.add(music)
    Occurrence.objects.create(event=gig, start=now + timedelta(days=2))

    market = Event.objects.create(
        title="Farmers Market", venue=hall, is_free=True,
        listing_type=Event.ListingType.SERIES,
        prominence=Event.Prominence.FEATURED,
        status=Event.Status.PUBLISHED,
    )
    for week in range(8):
        Occurrence.objects.create(event=market, start=now + timedelta(days=7 * week + 1))

    pottery = Event.objects.create(
        title="Pottery Class", venue=studio, price_min=180,
        is_commercial=True,
        listing_type=Event.ListingType.SERIES,
        prominence=Event.Prominence.BACKGROUND,
        status=Event.Status.PUBLISHED,
        organizer=Organizer.objects.create(name="Craft Studio", is_commercial=True),
    )
    pottery.categories.add(learning)
    for week in range(8):
        Occurrence.objects.create(event=pottery, start=now + timedelta(days=7 * week + 3))

    unpublished = Event.objects.create(title="Not Yet Reviewed", venue=hall)
    Occurrence.objects.create(event=unpublished, start=now + timedelta(days=4))

    return {"hall": hall, "studio": studio, "gig": gig, "market": market,
            "pottery": pottery, "unpublished": unpublished}


# --- collapsing ------------------------------------------------------------


@pytest.mark.django_db
def test_a_weekly_series_appears_once_not_once_per_week(client, world):
    """The whole reason listing_type exists.

    The market has eight scheduled dates; it must contribute one card.
    """
    response = client.get(reverse("index"))
    body = response.content.decode()

    assert body.count(">Farmers Market<") == 1


@pytest.mark.django_db
def test_series_shows_its_next_date(world):
    filters = EventFilter()
    market = filters.main_feed().get(title="Farmers Market")
    assert market.next_start == market.occurrences.order_by("start").first().start


# --- placement -------------------------------------------------------------


@pytest.mark.django_db
def test_background_programs_are_kept_out_of_the_main_feed(client, world):
    response = client.get(reverse("index"))
    feed_titles = [e.title for e in response.context["events"]]
    program_titles = [e.title for e in response.context["programs"]]

    assert "Farmers Market" in feed_titles
    assert "Pottery Class" not in feed_titles
    assert "Pottery Class" in program_titles


@pytest.mark.django_db
def test_featured_sorts_above_listed(client, world):
    response = client.get(reverse("index"))
    titles = [e.title for e in response.context["events"]]
    assert titles.index("Farmers Market") < titles.index("Open Mic Night")


@pytest.mark.django_db
def test_unpublished_events_are_never_shown(client, world):
    response = client.get(reverse("index"))
    assert "Not Yet Reviewed" not in response.content.decode()


# --- filters ---------------------------------------------------------------


@pytest.mark.django_db
def test_free_filter_excludes_paid(client, world):
    response = client.get(reverse("index"), {"free": "1"})
    everything = [e.title for e in response.context["events"]] + [
        e.title for e in response.context["programs"]
    ]
    assert "Pottery Class" not in everything


@pytest.mark.django_db
def test_category_filter_narrows(client, world):
    response = client.get(reverse("index"), {"category": "music"})
    assert [e.title for e in response.context["events"]] == ["Open Mic Night"]


@pytest.mark.django_db
def test_hide_commercial_filter(client, world):
    response = client.get(reverse("index"), {"noncommercial": "1"})
    programs = [e.title for e in response.context["programs"]]
    assert "Pottery Class" not in programs


@pytest.mark.django_db
def test_venue_filter(client, world):
    response = client.get(reverse("index"), {"venue": "craft-studio"})
    everything = [e.title for e in response.context["events"]] + [
        e.title for e in response.context["programs"]
    ]
    assert everything == ["Pottery Class"]


@pytest.mark.django_db
def test_search_matches_venue_name(client, world):
    response = client.get(reverse("index"), {"q": "Craft Studio"})
    everything = [e.title for e in response.context["events"]] + [
        e.title for e in response.context["programs"]
    ]
    assert "Pottery Class" in everything


@pytest.mark.django_db
def test_unknown_filter_values_are_ignored_not_fatal(client, world):
    """A hand-edited URL should widen the view, not 500."""
    response = client.get(
        reverse("index"), {"when": "next-tuesday-ish", "category": "not-a-category"}
    )
    assert response.status_code == 200
    assert response.context["filters"].when == ""
    assert response.context["filters"].categories == []


@pytest.mark.django_db
def test_today_filter_excludes_later_events(client, world):
    response = client.get(reverse("index"), {"when": "today"})
    # Everything in the fixture starts at least a day out.
    assert response.context["events"] == []


@pytest.mark.django_db
def test_querystring_round_trip_preserves_other_filters():
    filters = EventFilter(free_only=True, categories=["music"], when="week")
    qs = filters.querystring(when="month")
    assert "free=1" in qs
    assert "category=music" in qs
    assert "when=month" in qs
    assert "when=week" not in qs


# --- HTMX partial ----------------------------------------------------------


@pytest.mark.django_db
def test_htmx_request_returns_only_the_results_fragment(client, world):
    """The filter bar and map must keep their state across a filter change."""
    response = client.get(reverse("index"), {"free": "1"}, HTTP_HX_REQUEST="true")
    body = response.content.decode()

    assert 'id="results"' in body
    assert "<html" not in body.lower()
    assert 'id="filters"' not in body


@pytest.mark.django_db
def test_full_request_returns_the_whole_page(client, world):
    response = client.get(reverse("index"))
    body = response.content.decode()
    assert "<html" in body.lower()
    assert 'id="filters"' in body


# --- map -------------------------------------------------------------------


@pytest.mark.django_db
def test_geojson_groups_events_by_venue(client, world):
    response = client.get(reverse("events_geojson"))
    data = json.loads(response.content)

    assert data["type"] == "FeatureCollection"
    venues = {f["properties"]["venue"] for f in data["features"]}
    assert venues == {"Community Hall", "Craft Studio"}

    hall = next(f for f in data["features"] if f["properties"]["venue"] == "Community Hall")
    assert hall["properties"]["count"] == 2  # gig + market share a venue


@pytest.mark.django_db
def test_geojson_honours_the_same_filters_as_the_list(client, world):
    """Pins and results must never disagree."""
    response = client.get(reverse("events_geojson"), {"category": "music"})
    data = json.loads(response.content)
    venues = {f["properties"]["venue"] for f in data["features"]}
    assert venues == {"Community Hall"}


@pytest.mark.django_db
def test_geojson_skips_venues_without_coordinates(client, world):
    ungeocoded = Venue.objects.create(name="Somewhere", city="Anytown")
    event = Event.objects.create(
        title="Mystery", venue=ungeocoded, status=Event.Status.PUBLISHED
    )
    Occurrence.objects.create(event=event, start=timezone.now() + timedelta(days=1))

    data = json.loads(client.get(reverse("events_geojson")).content)
    assert "Somewhere" not in {f["properties"]["venue"] for f in data["features"]}


# --- detail ----------------------------------------------------------------


@pytest.mark.django_db
def test_detail_page_renders(client, world):
    response = client.get(
        reverse("event_detail", args=[world["gig"].slug])
    )
    assert response.status_code == 200
    assert "Open Mic Night" in response.content.decode()


@pytest.mark.django_db
def test_unpublished_event_detail_is_404(client, world):
    response = client.get(
        reverse("event_detail", args=[world["unpublished"].slug])
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_detail_emits_valid_schema_org_event(client, world):
    """This is how search engines surface the event; malformed means invisible."""
    response = client.get(reverse("event_detail", args=[world["gig"].slug]))
    data = json.loads(response.context["schema_json"])

    assert data["@type"] == "Event"
    assert data["name"] == "Open Mic Night"
    assert data["startDate"]
    assert data["location"]["@type"] == "Place"
    assert data["location"]["geo"]["latitude"] == 43.1
    assert data["offers"]["price"] == "0"


@pytest.mark.django_db
def test_the_site_has_no_concept_of_an_online_event(client, world):
    """Online listings are not filtered out — they cannot be expressed.

    Something happening "anywhere" has no local connection to check, which
    makes it the easiest thing to spam a community listing with. Keeping it out
    of the model is stronger than offering it and moderating it afterwards.
    """
    assert not hasattr(Event, "is_online")
    assert "is_online" not in {f.name for f in Event._meta.get_fields()}
    assert "online_url" not in {f.name for f in Event._meta.get_fields()}

    response = client.get(reverse("index"))
    assert 'name="online"' not in response.content.decode()

    data = json.loads(
        client.get(reverse("event_detail", args=[world["gig"].slug])).context[
            "schema_json"
        ]
    )
    assert data["eventAttendanceMode"].endswith("OfflineEventAttendanceMode")


# --- rendering hygiene -----------------------------------------------------


@pytest.mark.parametrize(
    "url_name,args",
    [("index", []), ("event_detail", ["open-mic-night"])],
)
@pytest.mark.django_db
def test_no_template_syntax_leaks_into_the_page(client, world, url_name, args):
    """Guard against a whole class of silent template bug.

    Django's `{# ... #}` comment is single-line only; spread one over two lines
    and it renders as visible body text. That shipped once already. The same
    check catches unrendered tags and unknown variables that slip through as
    literal braces.
    """
    body = client.get(reverse(url_name, args=args)).content.decode()

    # Strip legitimate inline script content, which may contain braces.
    for marker in ("{#", "#}", "{% ", " %}"):
        assert marker not in body, f"raw template syntax {marker!r} rendered in {url_name}"
