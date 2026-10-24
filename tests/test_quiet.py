"""The Quiet tier: on the site, and on the front page only when it is soon.

The front page of a busy instance hit its card cap with listings nobody would
call headline news — a library talk, a club's monthly meeting. Background was
the wrong home for those: it is title-ordered and dateless, built for standing
weekly programs. Quiet keeps the date and gives the front page a horizon.

Two rules are held here. The horizon is the *front page's* rule only: a venue
page, a category page or any active filter is a reader asking a narrower
question, and the answer must not leave a listing out for being quiet. And a
promotion from Quiet goes straight to Listed — Quiet is where a moderator puts
something to keep it off the front page, not a rung to climb twice.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.models import SiteConfig
from events.models import Category, Event, Occurrence, Venue
from moderation import services


@pytest.fixture
def town(db):
    now = timezone.now()
    library = Venue.objects.create(
        name="Public Library", city="Anytown",
        latitude=43.1, longitude=-80.2,
        geocode_status=Venue.GeocodeStatus.OK,
    )
    learning = Category.objects.get(slug="learning")

    def make(title, prominence, days):
        event = Event.objects.create(
            title=title, venue=library, status=Event.Status.PUBLISHED,
            prominence=prominence,
        )
        event.categories.add(learning)
        Occurrence.objects.create(event=event, start=now + timedelta(days=days))
        return event

    return {
        "now": now,
        "library": library,
        "concert": make("Spring Concert", Event.Prominence.LISTED, days=3),
        "soon": make("Internet Safety for Seniors", Event.Prominence.QUIET, days=3),
        "alone": make("Knitting Circle", Event.Prominence.QUIET, days=5),
        "far": make("Tax Clinic", Event.Prominence.QUIET, days=40),
    }


def set_theme(slug):
    config = SiteConfig.load()
    config.theme = slug
    config.save()


# --- the front page -----------------------------------------------------------


def test_quiet_listings_are_not_in_the_main_feed(town):
    assert town["soon"] not in Event.objects.in_main_feed()
    assert town["soon"] in Event.objects.quiet()


def test_the_front_page_folds_soon_quiet_listings_under_their_day(client, town):
    response = client.get(reverse("index"))
    context = response.context

    assert [e.title for e in context["events"]] == ["Spring Concert"]
    assert [e.title for e in context["quiet"]] == [
        "Internet Safety for Seniors", "Knitting Circle",
    ]
    assert context["quiet_folded"] is True

    first_day, second_day = context["days"]
    assert [e.title for e in first_day["events"]] == ["Spring Concert"]
    assert [e.title for e in first_day["quiet"]] == ["Internet Safety for Seniors"]
    assert first_day["count"] == 2
    # A day with nothing but quiet listings still gets its heading.
    assert second_day["events"] == []
    assert [e.title for e in second_day["quiet"]] == ["Knitting Circle"]

    body = response.content.decode()
    assert '<details class="le-fold' in body
    assert "Internet Safety for Seniors" in body


def test_the_front_page_leaves_out_quiet_listings_beyond_the_horizon(client, town):
    body = client.get(reverse("index")).content.decode()
    assert "Tax Clinic" not in body
    assert "Spring Concert" in body


def test_the_horizon_is_the_instance_s_to_set(client, town):
    config = SiteConfig.load()
    config.quiet_horizon_days = 60
    config.save()

    body = client.get(reverse("index")).content.decode()
    assert "Tax Clinic" in body


# --- anywhere a reader asked a narrower question -----------------------------


def test_a_venue_page_shows_quiet_listings_in_full(client, town):
    response = client.get(reverse("venue_detail", args=[town["library"].slug]))

    assert response.context["quiet_folded"] is False
    assert "Tax Clinic" in response.content.decode()
    assert '<details class="le-fold' not in response.content.decode()


def test_a_category_page_shows_quiet_listings_in_full(client, town):
    response = client.get(reverse("category_detail", args=["learning"]))
    assert "Tax Clinic" in response.content.decode()


@pytest.mark.parametrize(
    "params", [{"q": "tax"}, {"when": "month"}, {"category": "learning"}]
)
def test_an_active_filter_lifts_the_horizon(client, town, params):
    """A date range of thirty days with a listing missing from day forty is
    fine; a search for "tax" with the Tax Clinic missing is a bug."""
    if params == {"when": "month"}:
        Occurrence.objects.filter(event=town["far"]).update(
            start=town["now"] + timedelta(days=20)
        )
    response = client.get(reverse("index"), params)
    assert response.context["quiet_folded"] is False
    assert "Tax Clinic" in response.content.decode()


def test_the_htmx_fragment_follows_the_same_rule(client, town):
    body = client.get(
        reverse("index"), {"q": "tax"}, HTTP_HX_REQUEST="true"
    ).content.decode()
    assert "Tax Clinic" in body


# --- the river theme ---------------------------------------------------------


def test_river_folds_per_day_and_counts_the_fold(client, town):
    set_theme("river")
    body = client.get(reverse("index")).content.decode()

    assert body.count('<details class="le-fold">') == 2
    assert "+1 more" in body
    assert "2 events" in body  # the concert and the talk, on the same day
    # The filter panel's own disclosure is untouched by the fold.
    assert body.count('<details class="le-more"') == 1


def test_river_venue_page_opens_quiet_listings_out(client, town):
    set_theme("river")
    body = client.get(
        reverse("venue_detail", args=[town["library"].slug])
    ).content.decode()
    assert "le-fold" not in body
    assert "Tax Clinic" in body


# --- moderation ---------------------------------------------------------------


def test_promoting_from_quiet_lands_on_the_front_page(town, moderator):
    event = services.promote(town["soon"], moderator)
    assert event.prominence == Event.Prominence.LISTED


def test_the_tiers_sort_most_prominent_first():
    values = [p.value for p in Event.Prominence]
    assert values == sorted(values)
    assert Event.Prominence.QUIET < Event.Prominence.LISTED
    assert Event.Prominence.BACKGROUND < Event.Prominence.QUIET
