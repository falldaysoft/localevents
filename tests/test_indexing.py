"""What a search engine sees: robots.txt, the sitemap, venue and category pages.

The venue and category pages exist because the front page's `?venue=` and
`?category=` views are query strings on `/`, which a crawler treats as thin
copies of the home page. These tests hold the things that make them worth
indexing — and the one that makes a venue page safe to have at all: a venue
is created when someone submits, before anyone has reviewed it.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from content.models import Page
from core.models import SiteConfig
from events.models import Category, Event, Occurrence, Venue

BASE = "https://events.example.org"


@pytest.fixture(autouse=True)
def base_url(settings):
    settings.SITE_BASE_URL = BASE


@pytest.fixture
def world(db):
    now = timezone.now()
    hall = Venue.objects.create(
        name="Community Hall", address="1 Main St", city="Anytown",
        latitude=43.1, longitude=-80.2, geocode_status=Venue.GeocodeStatus.OK,
    )
    music = Category.objects.get(slug="music")

    gig = Event.objects.create(
        title="Open Mic Night", venue=hall,
        status=Event.Status.PUBLISHED, prominence=Event.Prominence.LISTED,
    )
    gig.categories.add(music)
    Occurrence.objects.create(event=gig, start=now + timedelta(days=2))

    pending = Event.objects.create(title="Not Yet Reviewed", venue=hall)
    pending.categories.add(music)
    Occurrence.objects.create(event=pending, start=now + timedelta(days=4))

    # Created by a submission nobody has approved: its only event is a draft.
    unreviewed = Venue.objects.create(name="Someone's Garage", address="9 Private Rd")
    draft = Event.objects.create(title="Garage Sale", venue=unreviewed)
    Occurrence.objects.create(event=draft, start=now + timedelta(days=1))

    # Hosted something once; nothing coming up.
    quiet = Venue.objects.create(name="Old Mill")
    past = Event.objects.create(
        title="Last Year's Fair", venue=quiet, status=Event.Status.PUBLISHED,
    )
    Occurrence.objects.create(event=past, start=now - timedelta(days=200))

    return {"hall": hall, "music": music, "gig": gig, "pending": pending,
            "unreviewed": unreviewed, "quiet": quiet, "past": past}


def set_theme(slug):
    config = SiteConfig.load()
    config.theme = slug
    config.save()


# --- robots.txt --------------------------------------------------------------


@pytest.mark.django_db
def test_robots_names_an_absolute_sitemap_and_touches_no_database(
    client, django_assert_num_queries
):
    with django_assert_num_queries(0):
        response = client.get("/robots.txt")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/plain")
    body = response.content.decode()
    assert f"Sitemap: {BASE}/sitemap.xml" in body
    for path in ("/admin/", "/moderate/", "/accounts/", "/claim/", "/submit/"):
        assert f"Disallow: {path}\n" in body


@pytest.mark.django_db
def test_robots_keeps_crawlers_off_filtered_front_pages(client):
    """Every combination of filters is a near-copy of `/`; the landing pages
    are the indexable form of the same views."""
    body = client.get("/robots.txt").content.decode()
    assert "Disallow: /?\n" in body
    assert "Disallow: /\n" not in body


# --- the sitemap ---------------------------------------------------------------


@pytest.mark.django_db
def test_sitemap_uses_the_configured_host_not_the_sites_table(client, world):
    """An unedited Sites row says example.com; a sitemap built from it lists
    someone else's domain."""
    body = client.get("/sitemap.xml").content.decode()
    assert f"<loc>{BASE}/</loc>" in body
    assert "example.com" not in body


@pytest.mark.django_db
def test_sitemap_lists_what_the_public_can_see(client, world):
    Page.objects.create(title="About", slug="about", is_published=True)
    Page.objects.create(title="Draft", slug="draft-page")

    body = client.get("/sitemap.xml").content.decode()

    assert f"{BASE}/events/{world['gig'].slug}/" in body
    assert f"{BASE}/venues/{world['hall'].slug}/" in body
    assert f"{BASE}/categories/music/" in body
    assert f"{BASE}/venues/" in body
    assert f"{BASE}/categories/" in body
    assert f"{BASE}/p/about/" in body

    assert world["pending"].slug not in body
    assert world["unreviewed"].slug not in body
    assert "draft-page" not in body


@pytest.mark.django_db
def test_sitemap_drops_events_long_past(client, world):
    body = client.get("/sitemap.xml").content.decode()
    assert world["past"].slug not in body


# --- venue pages --------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("theme", ["classic", "river"])
def test_venue_page_lists_published_events_only(client, world, theme):
    set_theme(theme)
    response = client.get(reverse("venue_detail", args=[world["hall"].slug]))
    assert response.status_code == 200
    body = response.content.decode()
    assert "Community Hall" in body
    assert "1 Main St" in body
    assert "Open Mic Night" in body
    assert "Not Yet Reviewed" not in body
    assert f'<link rel="canonical" href="{BASE}/venues/{world["hall"].slug}/">' in body
    assert '"@type": "Place"' in body
    assert "<title>Events at Community Hall — " in body
    assert 'name="robots"' not in body


@pytest.mark.django_db
def test_a_venue_with_no_published_event_does_not_exist(client, world):
    """Venues are created at submission, before review. A page for one would
    publish a rejected submitter's address."""
    response = client.get(reverse("venue_detail", args=[world["unreviewed"].slug]))
    assert response.status_code == 404


@pytest.mark.django_db
def test_a_quiet_venue_still_renders_but_asks_not_to_be_indexed(client, world):
    """Links to a venue outlive a season; a thin page should not rank."""
    response = client.get(reverse("venue_detail", args=[world["quiet"].slug]))
    assert response.status_code == 200
    body = response.content.decode()
    assert "Nothing scheduled here right now." in body
    assert '<meta name="robots" content="noindex">' in body


@pytest.mark.django_db
@pytest.mark.parametrize("theme", ["classic", "river"])
def test_venue_index_lists_only_venues_with_something_on(client, world, theme):
    set_theme(theme)
    body = client.get(reverse("venue_list")).content.decode()
    assert reverse("venue_detail", args=[world["hall"].slug]) in body
    assert world["quiet"].slug not in body
    assert world["unreviewed"].slug not in body


@pytest.mark.django_db
@pytest.mark.parametrize("theme", ["classic", "river"])
def test_event_page_links_to_its_venue_and_categories(client, world, theme):
    set_theme(theme)
    body = client.get(reverse("event_detail", args=[world["gig"].slug])).content.decode()
    assert reverse("venue_detail", args=[world["hall"].slug]) in body
    assert reverse("category_detail", args=["music"]) in body


# --- category pages -----------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("theme", ["classic", "river"])
def test_category_page_lists_its_published_events(client, world, theme):
    set_theme(theme)
    response = client.get(reverse("category_detail", args=["music"]))
    assert response.status_code == 200
    body = response.content.decode()
    assert "Open Mic Night" in body
    assert "Not Yet Reviewed" not in body
    assert f'<link rel="canonical" href="{BASE}/categories/music/">' in body
    assert "<title>Music events — " in body


@pytest.mark.django_db
def test_an_inactive_category_has_no_page(client, world):
    Category.objects.filter(slug="music").update(is_active=False)
    assert client.get(reverse("category_detail", args=["music"])).status_code == 404


@pytest.mark.django_db
def test_an_empty_category_asks_not_to_be_indexed(client, world):
    body = client.get(reverse("category_detail", args=["learning"])).content.decode()
    assert '<meta name="robots" content="noindex">' in body


@pytest.mark.django_db
@pytest.mark.parametrize("theme", ["classic", "river"])
def test_category_index_counts_upcoming_events(client, world, theme):
    set_theme(theme)
    body = client.get(reverse("category_list")).content.decode()
    assert reverse("category_detail", args=["music"]) in body
    assert "1 event" in body


@pytest.mark.django_db
@pytest.mark.parametrize("theme", ["classic", "river"])
def test_footer_links_the_landing_indexes(client, world, theme):
    """Crawlers find pages by links; the footer is on every page."""
    set_theme(theme)
    body = client.get(reverse("index")).content.decode()
    assert f'href="{reverse("venue_list")}"' in body
    assert f'href="{reverse("category_list")}"' in body
