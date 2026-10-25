"""Browse pages come in pages of whole days.

The front page used to take the first 120 cards and stop, which on a busy
instance meant everything past about the fourth week was simply not there —
no "more", no hint. A page now holds roughly `PAGE_CARDS` cards and ends
between two days, and a "Later" link carries on from the day it stopped at.

The rules held here: a day is never split across two pages, a series appears
once (at its next date) however many pages it spans, featured listings and
programs sit on the first page only, the count is the whole answer and not
this page of it, and folded quiet listings cost nothing against the budget.
"""

from datetime import date, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from events.models import Category, Event, Occurrence, Venue
from web import views


@pytest.fixture(autouse=True)
def small_pages(request, monkeypatch):
    monkeypatch.setattr(views, "PAGE_CARDS", 3)
    # The rest-of-the-week floor would put every few-day fixture on one page,
    # so it is off unless a test asks for it.
    if "week_floor" not in request.fixturenames:
        monkeypatch.setattr(views, "_first_page_floor", lambda today: today)


@pytest.fixture
def week_floor():
    """Marker: keep the real first-page floor."""


@pytest.fixture
def hall(db):
    return Venue.objects.create(name="Community Hall", city="Anytown")


def noon(days):
    """Midday, local time, `days` from today — clear of any day boundary."""
    return timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=days)


@pytest.fixture
def make(hall):
    def make(title, *days, prominence=Event.Prominence.LISTED, **fields):
        event = Event.objects.create(
            title=title, venue=hall, status=Event.Status.PUBLISHED,
            prominence=prominence, **fields,
        )
        for day in days:
            Occurrence.objects.create(event=event, start=noon(day))
        return event

    return make


def titles(context):
    return [e.title for day in context["days"] for e in day["events"] + day["quiet"]]


def follow(client, url="/"):
    """Every page of a listing, by following "Later" until it stops."""
    pages = []
    while True:
        context = client.get(url).context
        pages.append(context)
        if not context["next_from"]:
            return pages
        query = context.get("page_query")
        url = f"{url.split('?')[0]}?{query + '&' if query else ''}from={context['next_from'].isoformat()}"


def test_a_page_ends_between_two_days_and_later_carries_on(client, make):
    for day in (1, 2, 3):
        make(f"Day {day} morning", day)
        make(f"Day {day} evening", day)

    pages = follow(client)

    assert [titles(p) for p in pages] == [
        ["Day 1 morning", "Day 1 evening"],
        ["Day 2 morning", "Day 2 evening"],
        ["Day 3 morning", "Day 3 evening"],
    ]
    # No day heading appears on two pages.
    dates = [day["date"] for p in pages for day in p["days"]]
    assert len(dates) == len(set(dates))


def test_a_day_busier_than_a_page_is_shown_whole(client, make):
    for n in range(5):
        make(f"Fair stall {n}", 1)
    make("Next day", 2)

    first, second = follow(client)

    assert len(titles(first)) == 5
    assert titles(second) == ["Next day"]


def test_no_later_link_when_the_busy_first_day_is_the_last(client, make):
    for n in range(5):
        make(f"Fair stall {n}", 1)

    (only,) = follow(client)
    assert only["next_from"] is None
    assert "Later" not in client.get("/").content.decode()


def test_a_series_appears_once_at_its_next_date(client, make):
    make("Weekly class", 1, 8, 15, 22)
    for day in (2, 3, 9, 10, 16, 17):
        make(f"One-off {day}", day)

    everything = [t for p in follow(client) for t in titles(p)]
    assert everything.count("Weekly class") == 1
    assert len(everything) == 7


def test_featured_and_programs_are_on_the_first_page_only(client, make):
    make("Big festival", 20, prominence=Event.Prominence.FEATURED)
    make("Tuesday bridge club", 1, prominence=Event.Prominence.BACKGROUND)
    for day in (1, 2, 3, 4):
        make(f"One-off {day}a", day)
        make(f"One-off {day}b", day)

    first, *rest = follow(client)

    assert [e.title for e in first["featured"]] == ["Big festival"]
    assert [e.title for e in first["programs"]] == ["Tuesday bridge club"]
    assert rest
    for page in rest:
        assert page["featured"] == [] and page["programs"] == []
    everything = [t for p in follow(client) for t in titles(p)]
    assert "Big festival" not in everything


def test_the_count_is_the_whole_answer_not_this_page(client, make):
    for day in range(1, 8):
        make(f"One-off {day}", day)

    response = client.get("/")
    assert response.context["result_count"] == 7
    assert "7 events" in response.content.decode()


def test_folded_quiet_listings_do_not_use_up_the_page(client, make):
    for n in range(6):
        make(f"Library talk {n}", 1, prominence=Event.Prominence.QUIET)
    make("Concert", 1)
    make("Play", 2)

    first = client.get("/").context
    assert first["quiet_folded"] is True
    assert set(titles(first)) >= {"Concert", "Play"}
    assert first["next_from"] is None


def test_unfolded_quiet_listings_count_like_any_card(client, make, hall):
    for n in range(4):
        make(f"Library talk {n}", 1, prominence=Event.Prominence.QUIET)
    make("Play", 2)

    url = reverse("venue_detail", args=[hall.slug])
    first, second = follow(client, url)
    assert first["quiet_folded"] is False
    assert titles(second) == ["Play"]


def test_later_keeps_the_readers_filters(client, make):
    music = Category.objects.get(slug="music")
    for day in (1, 2, 3):
        for n in range(2):
            make(f"Gig {day}{n}", day).categories.add(music)
    make("Not music", 1)

    response = client.get(f"/?category={music.slug}")
    body = response.content.decode()
    later = response.context["next_from"].isoformat()
    assert f"category={music.slug}&amp;from={later}" in body

    second = client.get(f"/?category={music.slug}&from={later}").context
    assert "Not music" not in titles(second)
    assert titles(second) and all(t.startswith("Gig") for t in titles(second))
    assert "Back to today" in client.get(f"/?category={music.slug}&from={later}").content.decode()


@pytest.mark.parametrize("value", ["", "not-a-date", "2001-01-01"])
def test_a_bad_or_past_from_is_the_first_page(client, make, value):
    make("Soon", 1)
    make("Featured", 2, prominence=Event.Prominence.FEATURED)

    context = client.get(f"/?from={value}").context
    assert context["page_from"] is None
    assert [e.title for e in context["featured"]] == ["Featured"]


def test_a_page_past_the_end_says_so(client, make):
    make("Soon", 1)
    response = client.get(f"/?from={noon(30).date().isoformat()}")
    body = response.content.decode()
    assert "Nothing further from then." in body
    assert "Back to today" in body


def test_the_category_page_pages_too(client, make):
    music = Category.objects.get(slug="music")
    for day in (1, 2, 3):
        for n in range(2):
            make(f"Gig {day}{n}", day).categories.add(music)

    pages = follow(client, reverse("category_detail", args=[music.slug]))
    assert len(pages) == 3


def test_my_events_is_not_paged(client, make):
    events = [make(f"Marked {day}{n}", day) for day in (1, 2, 3) for n in range(2)]
    for event in events:
        client.post(reverse("event_interest", args=[event.slug]))

    context = client.get(reverse("my_events")).context
    assert len(titles(context)) == 6
    assert context["past"] == []


@pytest.mark.parametrize("theme", ["classic", "river"])
def test_both_themes_render_the_later_link(client, make, theme):
    from core.models import SiteConfig

    config = SiteConfig.load()
    config.theme = theme
    config.save()
    for day in (1, 2, 3):
        make(f"Day {day} morning", day)
        make(f"Day {day} evening", day)

    body = client.get("/").content.decode()
    assert f"from={noon(2).date().isoformat()}" in body
    assert "Later" in body


def test_a_quiet_only_first_day_does_not_strand_a_busy_second_day(client, make):
    make("Library talk", 1, prominence=Event.Prominence.QUIET)
    for n in range(5):
        make(f"Fair stall {n}", 2)
    make("Day three", 3)

    first, second = follow(client)
    assert len([t for t in titles(first) if t.startswith("Fair stall")]) == 5
    assert titles(second) == ["Day three"]


# --- the featured cap ---------------------------------------------------------


def pinned(client, url="/"):
    return [e.title for e in client.get(url).context["featured"]]


def test_far_off_featured_listings_only_fill_the_cap(client, make):
    make("Soon A", 2, prominence=Event.Prominence.FEATURED)
    make("Soon B", 5, prominence=Event.Prominence.FEATURED)
    make("Far C", 40, prominence=Event.Prominence.FEATURED)
    make("Far D", 50, prominence=Event.Prominence.FEATURED)

    assert pinned(client) == ["Soon A", "Soon B", "Far C"]


def test_every_imminent_featured_listing_is_pinned_past_the_cap(client, make):
    for n in range(4):
        make(f"Soon {n}", n + 1, prominence=Event.Prominence.FEATURED)
    make("Far", 40, prominence=Event.Prominence.FEATURED)

    assert pinned(client) == ["Soon 0", "Soon 1", "Soon 2", "Soon 3"]


def test_a_featured_listing_past_the_cap_joins_the_day_list(client, make):
    for n in range(3):
        make(f"Soon {n}", n + 1, prominence=Event.Prominence.FEATURED)
    make("Far festival", 40, prominence=Event.Prominence.FEATURED)

    everything = [t for p in follow(client) for t in titles(p)]
    assert everything == ["Far festival"]
    assert client.get("/").context["result_count"] == 4


# --- the first page covers the week ------------------------------------------


@pytest.mark.parametrize(
    "today, floor",
    [
        (date(2026, 10, 19), date(2026, 10, 26)),  # Monday: through Sunday the 25th
        (date(2026, 10, 23), date(2026, 10, 26)),  # Friday: this weekend
        (date(2026, 10, 24), date(2026, 11, 2)),  # Saturday: through next Sunday
        (date(2026, 10, 25), date(2026, 11, 2)),  # Sunday: through next Sunday
    ],
)
def test_the_first_page_floor(today, floor, week_floor):
    assert views._first_page_floor(today) == floor


def test_the_first_page_runs_to_the_floor_however_busy(client, make, week_floor):
    floor = views._first_page_floor(timezone.localdate())
    span = (floor - timezone.localdate()).days
    for day in range(1, span + 3):
        make(f"Day {day} morning", day)
        make(f"Day {day} evening", day)

    first, *rest = follow(client)
    assert first["next_from"] == floor
    assert len(titles(first)) == 2 * (span - 1)
    assert rest and all(len(titles(p)) == 2 for p in rest)


def test_a_later_page_has_no_floor(client, make, week_floor):
    start = views._first_page_floor(timezone.localdate())
    offset = (start - timezone.localdate()).days
    for day in range(offset, offset + 3):
        make(f"Day {day} morning", day)
        make(f"Day {day} evening", day)

    context = client.get(f"/?from={start.isoformat()}").context
    assert len(titles(context)) == 2
