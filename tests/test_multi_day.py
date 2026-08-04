"""Events that run longer than a moment.

Two shapes hide under "multi-day", and they fail differently:

A **span** is one occurrence that runs continuously across days — a festival
from Friday evening to Sunday afternoon. Its failure mode is disappearance.
Every query used to ask "does it *start* inside the window", so on Saturday the
festival was gone from Today, from This Weekend, from the map and from the
feed, while it was actually happening.

A **repeat** is several occurrences with different hours — a market open
Fridays 9–2 and Saturdays 7–2. Its failure mode is silent truncation: the form
had one "ends at" box and a textarea of bare dates, so every closing time but
the first was written to the database as null.

Both are checked here rather than spread across the browse and submission
files, because what they have in common — an occurrence has a duration, and it
matters — is the thing that would be quietly re-broken.
"""

from datetime import timedelta

import pytest
from django.template import Context, Template
from django.urls import reverse
from django.utils import timezone

from enrichment import structured
from events.models import Event, Occurrence, Venue
from web.filters import EventFilter


@pytest.fixture
def hall(db):
    return Venue.objects.create(
        name="Community Hall", city="Anytown",
        latitude=43.1, longitude=-80.2,
        geocode_status=Venue.GeocodeStatus.OK,
    )


def _published(title, venue, **kwargs):
    return Event.objects.create(
        title=title, venue=venue, status=Event.Status.PUBLISHED,
        prominence=Event.Prominence.LISTED, **kwargs
    )


@pytest.fixture
def festival(hall):
    """Started yesterday evening, runs until tomorrow afternoon."""
    now = timezone.now()
    event = _published("Riverside Festival", hall)
    Occurrence.objects.create(
        event=event,
        start=now - timedelta(days=1),
        end=now + timedelta(days=1),
    )
    return event


# --- a span stays visible while it runs ------------------------------------


@pytest.mark.django_db
def test_a_festival_under_way_is_still_in_the_feed(client, festival):
    """The bug this whole change exists for.

    Filtering on start alone drops an event at the moment it is most worth
    showing — while it is on.
    """
    titles = [e.title for e in client.get(reverse("index")).context["events"]]
    assert "Riverside Festival" in titles


@pytest.mark.django_db
def test_a_festival_under_way_answers_to_today(client, festival):
    response = client.get(reverse("index"), {"when": "today"})
    assert [e.title for e in response.context["events"]] == ["Riverside Festival"]


@pytest.mark.django_db
def test_a_festival_that_has_finished_is_gone(client, hall):
    now = timezone.now()
    event = _published("Last Year's Fair", hall)
    Occurrence.objects.create(
        event=event, start=now - timedelta(days=3), end=now - timedelta(days=1)
    )

    assert client.get(reverse("index")).context["events"] == []


@pytest.mark.django_db
def test_an_instant_with_no_end_still_behaves_as_before(client, hall):
    """The overlap test must not quietly widen the ordinary case.

    An occurrence with no stated end is over the moment it starts, exactly as
    it was when the query asked about `start` alone.
    """
    event = _published("Open Mic Night", hall)
    Occurrence.objects.create(event=event, start=timezone.now() - timedelta(minutes=1))

    assert client.get(reverse("index")).context["events"] == []


@pytest.mark.django_db
def test_a_venue_hosting_something_right_now_counts_as_active(festival):
    from web.filters import active_venues

    assert [v.name for v in active_venues()] == ["Community Hall"]


@pytest.mark.django_db
def test_the_event_page_shows_a_span_that_is_under_way(client, festival):
    response = client.get(reverse("event_detail", args=[festival.slug]))

    assert response.context["next_occurrence"] is not None
    assert "On now" in response.content.decode()


@pytest.mark.django_db
def test_the_map_carries_the_end_as_well_as_the_start(client, festival):
    import json

    data = json.loads(client.get("/events.geojson").content)
    event = data["features"][0]["properties"]["events"][0]

    assert event["start"] and event["end"]


# --- start and end must come from the same occurrence ----------------------


@pytest.mark.django_db
def test_the_annotated_end_belongs_to_the_annotated_start(hall):
    """Why this is a subquery and not two aggregates.

    Min(start) and Min(end) over the same set straddle two different dates the
    moment one occurrence has an end and another does not, because Min skips
    nulls — so the card would print one date's start beside another's finish.
    """
    now = timezone.now()
    event = _published("Two Nights", hall)
    soon = now + timedelta(days=1)
    Occurrence.objects.create(event=event, start=soon)  # no end
    Occurrence.objects.create(
        event=event, start=now + timedelta(days=5), end=now + timedelta(days=5, hours=2)
    )

    annotated = EventFilter().main_feed().get(pk=event.pk)
    assert annotated.next_start == soon
    assert annotated.next_end is None


# --- a repeat keeps each date's own hours ----------------------------------


@pytest.mark.django_db
def test_each_date_of_a_market_keeps_its_own_closing_time(hall):
    """Fridays 9–2, Saturdays 7–2. Two rows, four distinct times."""
    friday = (timezone.now() + timedelta(days=3)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    market = _published(
        "Farmers Market", hall, listing_type=Event.ListingType.SERIES
    )
    Occurrence.objects.create(
        event=market, start=friday, end=friday.replace(hour=14)
    )
    saturday = (friday + timedelta(days=1)).replace(hour=7)
    Occurrence.objects.create(
        event=market, start=saturday, end=saturday.replace(hour=14)
    )

    hours = [
        (timezone.localtime(o.start).hour, timezone.localtime(o.end).hour)
        for o in market.occurrences.order_by("start")
    ]
    assert hours == [(9, 14), (7, 14)]


@pytest.mark.django_db
def test_an_end_before_its_start_cannot_be_stored(hall):
    """The database is the last line, below any form.

    Everything downstream — the range formatter, `has_ended`, the overlap
    query — assumes an end that is genuinely after its start.
    """
    from django.db.utils import IntegrityError

    event = _published("Backwards", hall)
    start = timezone.now() + timedelta(days=1)

    with pytest.raises(IntegrityError):
        Occurrence.objects.create(event=event, start=start, end=start - timedelta(hours=1))


# --- structured extraction --------------------------------------------------


def _page(*events):
    import json

    scripts = "".join(
        f'<script type="application/ld+json">{json.dumps(e)}</script>'
        for e in events
    )
    return f"<html><head>{scripts}</head><body></body></html>"


def test_a_span_in_schema_org_stays_one_occurrence():
    draft = structured.extract(
        _page(
            {
                "@type": "Event",
                "name": "Riverside Festival",
                "startDate": "2099-07-10T18:00",
                "endDate": "2099-07-12T17:00",
            }
        )
    )

    assert len(draft.occurrences) == 1
    assert draft.occurrences[0].end.day - draft.occurrences[0].start.day == 2


def test_every_node_naming_the_same_event_contributes_a_date():
    """A recurring listing is routinely one Event node per date.

    Reading only the first turned a market that runs every Saturday into a
    market that ran once, with nothing to tell the submitter dates were lost.
    """
    draft = structured.extract(
        _page(
            {
                "@type": "Event",
                "name": "Farmers Market",
                "startDate": "2099-07-11T09:00",
                "endDate": "2099-07-11T14:00",
            },
            {
                "@type": "Event",
                "name": "Farmers Market",
                "startDate": "2099-07-12T07:00",
                "endDate": "2099-07-12T14:00",
            },
            {"@type": "Event", "name": "Something Else", "startDate": "2099-08-01T10:00"},
        )
    )

    assert [o.start.hour for o in draft.occurrences] == [9, 7]
    assert [o.end.hour for o in draft.occurrences] == [14, 14]
    assert draft.is_series
    assert "2 dates" in draft.notes_for_submitter


def test_an_end_equal_to_its_start_is_read_as_no_end_at_all():
    """Publishers stamp endDate == startDate to mean nothing about duration.

    Storing it would violate the occurrence's own check constraint, so the
    honest reading is "not stated".
    """
    draft = structured.extract(
        _page(
            {
                "@type": "Event",
                "name": "Talk",
                "startDate": "2099-07-10T18:00",
                "endDate": "2099-07-10T18:00",
            }
        )
    )

    assert draft.occurrences[0].end is None


# --- formatting -------------------------------------------------------------


def _render(start, end=None):
    template = Template(
        "{% load event_dates %}{% occurrence_when start end %}"
    )
    return template.render(Context({"start": start, "end": end}))


@pytest.mark.django_db
def test_a_range_within_one_day_names_the_day_once():
    start = timezone.make_aware(timezone.datetime(2099, 7, 11, 9, 0))
    assert _render(start, start.replace(hour=14)) == "Sat 11 Jul, 9:00 a.m. – 2:00 p.m."


@pytest.mark.django_db
def test_a_range_across_days_names_both_days():
    """The failure this replaced printed "6 p.m. – 5 p.m." and left the reader
    to guess which day the second time was on."""
    start = timezone.make_aware(timezone.datetime(2099, 7, 10, 18, 0))
    end = timezone.make_aware(timezone.datetime(2099, 7, 12, 17, 0))

    assert _render(start, end) == "Fri 10 Jul, 6:00 p.m. – Sun 12 Jul, 5:00 p.m."


@pytest.mark.django_db
def test_no_end_prints_only_the_start():
    start = timezone.make_aware(timezone.datetime(2099, 7, 10, 18, 0))
    assert _render(start) == "Fri 10 Jul, 6:00 p.m."
