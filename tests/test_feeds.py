"""The RSS feed at /events.rss.

Both index templates advertised it for months before it existed. What it must
hold: only published listings, in the order they were published rather than
edited, with absolute links on the instance's own domain.
"""

from datetime import timedelta
from xml.etree import ElementTree

import pytest
from django.urls import reverse
from django.utils import timezone

from events.models import Category, Event, Occurrence, Venue

BASE = "https://events.example.org"


@pytest.fixture(autouse=True)
def base_url(settings):
    settings.SITE_BASE_URL = BASE


def _event(title, status=Event.Status.PUBLISHED, days=3, **fields):
    event = Event.objects.create(title=title, status=status, **fields)
    Occurrence.objects.create(event=event, start=timezone.now() + timedelta(days=days))
    return event


def _items(client, query=""):
    response = client.get(reverse("events_rss") + query)
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/rss+xml")
    channel = ElementTree.fromstring(response.content).find("channel")
    return channel, channel.findall("item")


@pytest.mark.django_db
def test_only_published_events_appear(client):
    _event("Open Mic Night")
    _event("Awaiting Review", status=Event.Status.PENDING)
    _event("Turned Down", status=Event.Status.REJECTED)
    _event("Half Written", status=Event.Status.DRAFT)

    _, items = _items(client)

    assert [i.findtext("title") for i in items] == ["Open Mic Night"]


@pytest.mark.django_db
def test_newest_published_first_and_an_edit_does_not_resurface(client):
    older = _event("Older")
    newer = _event("Newer")
    Event.objects.filter(pk=older.pk).update(
        published_at=timezone.now() - timedelta(days=5)
    )
    older.refresh_from_db()
    older.summary = "Typo fixed"
    older.save()

    _, items = _items(client)

    assert [i.findtext("title") for i in items] == ["Newer", "Older"]
    assert newer.published_at is not None


@pytest.mark.django_db
def test_links_are_absolute_on_the_site_base_url(client):
    event = _event("Craft Fair")

    channel, items = _items(client)

    assert channel.findtext("link") == f"{BASE}/"
    assert items[0].findtext("link") == f"{BASE}/events/{event.slug}/"


@pytest.mark.django_db
def test_item_carries_the_date_and_venue(client):
    hall = Venue.objects.create(name="Community Hall")
    _event("Bake Sale", venue=hall, summary="Pies and tarts")

    _, items = _items(client)
    body = items[0].findtext("description")

    assert "Community Hall" in body
    assert "Pies and tarts" in body
    assert items[0].findtext("pubDate")


@pytest.mark.django_db
def test_a_listing_that_is_over_is_left_out(client):
    _event("Last Year's Fair", days=-200)

    _, items = _items(client)

    assert items == []


@pytest.mark.django_db
def test_category_filter_makes_its_own_feed(client):
    music = Category.objects.get(slug="music")
    gig = _event("Gig")
    gig.categories.add(music)
    _event("Book Club")

    channel, items = _items(client, "?category=music")

    assert [i.findtext("title") for i in items] == ["Gig"]
    assert music.name in channel.findtext("title")


@pytest.mark.django_db
def test_venue_feed_does_not_name_an_unreviewed_venue(client):
    garage = Venue.objects.create(name="Someone's Garage")
    _event("Garage Sale", status=Event.Status.PENDING, venue=garage)

    channel, items = _items(client, f"?venue={garage.slug}")

    assert items == []
    assert "Garage" not in channel.findtext("title")


@pytest.mark.django_db
def test_pages_advertise_their_feeds(client):
    hall = Venue.objects.create(name="Community Hall")
    _event("Open Mic Night", venue=hall)

    index = client.get(reverse("index")).content.decode()
    venue = client.get(reverse("venue_detail", args=[hall.slug])).content.decode()
    category = client.get(reverse("category_detail", args=["music"])).content.decode()

    assert 'type="application/rss+xml"' in index
    assert 'href="/events.rss"' in index
    assert f'href="/events.rss?venue={hall.slug}"' in venue
    assert 'href="/events.rss?category=music"' in category
