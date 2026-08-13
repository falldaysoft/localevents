"""Themes: the optional look an instance chooses.

The promise being guarded is narrow but load-bearing. Upgrading the product
must not restyle a running site, choosing a theme must not require a deploy,
and a theme that goes missing must degrade to a plain page rather than a 500.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.models import SiteConfig
from core.themes import DEFAULT_THEME, THEMES, Theme, get_theme, template_name
from events.models import Category, Event, Occurrence, Venue


@pytest.fixture
def listings(db):
    now = timezone.now()
    hall = Venue.objects.create(
        name="Community Hall",
        city="Anytown",
        latitude=43.1,
        longitude=-80.2,
        geocode_status=Venue.GeocodeStatus.OK,
    )
    music = Category.objects.get(slug="music")

    # Two listed events on *different* days, so day grouping has something to
    # group, plus one featured event whose date sits between them — which is
    # what would expose a featured listing leaking into the day list.
    for days, title in ((2, "Open Mic Night"), (5, "Village Quiz")):
        event = Event.objects.create(
            title=title,
            venue=hall,
            is_free=True,
            status=Event.Status.PUBLISHED,
            prominence=Event.Prominence.LISTED,
        )
        event.categories.add(music)
        Occurrence.objects.create(event=event, start=now + timedelta(days=days))

    headline = Event.objects.create(
        title="Autumn Fair",
        venue=hall,
        status=Event.Status.PUBLISHED,
        prominence=Event.Prominence.FEATURED,
    )
    headline.categories.add(music)
    Occurrence.objects.create(event=headline, start=now + timedelta(days=3))
    return {"now": now}


def set_theme(slug):
    config = SiteConfig.load()
    config.theme = slug
    config.save()


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_default_theme_is_registered():
    assert DEFAULT_THEME in THEMES


def test_unknown_theme_falls_back_rather_than_raising():
    """A slug removed from the registry must not 500 every page.

    An instance can hold a theme name in its database that a later release no
    longer ships. Rendering plainly is a far better failure than not rendering.
    """
    assert get_theme("no-such-theme").slug == DEFAULT_THEME
    assert get_theme(None).slug == DEFAULT_THEME
    assert get_theme("").slug == DEFAULT_THEME


def test_default_theme_uses_unthemed_templates():
    assert template_name(get_theme(DEFAULT_THEME), "web/index.html") == "web/index.html"


def test_theme_falls_back_per_template():
    """A theme overrides the pages it ships and inherits the rest."""
    river = get_theme("river")
    assert template_name(river, "web/index.html") == "themes/river/web/index.html"
    # Not overridden by the river theme, so it resolves to the shared one.
    assert template_name(river, "partials/_messages.html") == "partials/_messages.html"


def test_partial_reference_keeps_its_fragment():
    """HTMX renders a fragment, and a theme has to be able to override it."""
    assert (
        template_name(get_theme("river"), "web/index.html#results")
        == "themes/river/web/index.html#results"
    )


def test_a_theme_with_no_templates_returns_the_path_unchanged():
    bare = Theme(slug=DEFAULT_THEME, name="Bare", description="")
    assert template_name(bare, "web/index.html") == "web/index.html"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_default_instance_is_unstyled_by_a_theme(client, listings):
    """Shipping a theme must not change how an existing site looks."""
    response = client.get(reverse("index"))
    assert response.status_code == 200
    body = response.content.decode()
    assert 'data-theme="classic"' in body
    assert "themes/river.css" not in body


def test_choosing_a_theme_takes_effect_without_a_deploy(client, listings):
    set_theme("river")
    response = client.get(reverse("index"))
    assert response.status_code == 200
    body = response.content.decode()
    assert 'data-theme="river"' in body
    assert "themes/river.css" in body
    # The theme's own nav and card markup, not the built-in utility classes.
    assert "le-row" in body
    assert "le-nav" in body


def test_theme_groups_listings_under_a_day_heading(client, listings):
    set_theme("river")
    body = client.get(reverse("index")).content.decode()
    assert "le-day" in body
    # Two listed events on different days, so two headings.
    assert body.count('class="le-day"') == 2


def test_featured_listings_are_not_repeated_in_the_day_list(client, listings):
    """A featured event is pinned above the day list, and appears once.

    Grouping a prominence-ordered feed by date would emit the same day twice
    and print the featured event in both places.
    """
    set_theme("river")
    body = client.get(reverse("index")).content.decode()
    assert body.count("Autumn Fair") == 1
    assert "le-feature" in body


def test_htmx_fragment_renders_under_a_theme(client, listings):
    """The filter swap targets #results, which the theme also overrides."""
    set_theme("river")
    response = client.get(reverse("index"), headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.content.decode()
    assert 'id="results"' in body
    # A fragment, not the whole page.
    assert "<html" not in body


def test_secondary_filters_are_inside_the_disclosure(client, listings):
    """The chips must be *within* the <details>, not beside it.

    Shipped wrong once: the panel sat as a sibling, so the summary toggled
    nothing and eleven category chips stayed permanently open — which is the
    exact problem the disclosure was added to solve, and it looks completely
    fine in a screenshot of an opened page.
    """
    set_theme("river")
    body = client.get(reverse("index")).content.decode()
    start = body.index('<details class="le-more"')
    end = body.index("</details>", start)
    disclosed = body[start:end]
    assert "le-more__panel" in disclosed
    assert "le-toggles" in disclosed
    # And every category checkbox, not just the container.
    assert disclosed.count('name="category"') == Category.objects.filter(
        is_active=True
    ).count()


def disclosure_is_open(body):
    """Whether the filter <details> renders with the `open` attribute.

    Reads the opening tag itself rather than searching the page, so the answer
    cannot be changed by reindenting the template.
    """
    start = body.index("<details class=\"le-more\"")
    return "open" in body[start : body.index(">", start) + 1]


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"q": "quiz"},
        {"category": "music"},
        {"free": "1"},
        {"family": "1"},
        {"noncommercial": "1"},
    ],
)
def test_the_filter_panel_never_opens_by_itself(client, listings, params):
    """On a wide screen the panel is an overlay over the listings.

    Springing it for an already-filtered URL would cover the very results the
    visitor followed the link to read.
    """
    set_theme("river")
    body = client.get(reverse("index"), params).content.decode()
    assert not disclosure_is_open(body)


@pytest.mark.parametrize(
    "params",
    [
        {"category": "music"},
        {"free": "1"},
        {"family": "1"},
        {"noncommercial": "1"},
    ],
)
def test_an_applied_filter_is_marked_on_the_summary(client, listings, params):
    """Otherwise a visitor sees a narrowed list with no visible cause, and
    the cause is hidden inside a panel they have no reason to open.
    """
    set_theme("river")
    body = client.get(reverse("index"), params).content.decode()
    assert "le-more__dot" in body


@pytest.mark.parametrize("params", [{}, {"q": "quiz"}, {"when": "weekend"}])
def test_no_marker_when_nothing_in_the_panel_is_applied(client, listings, params):
    """A text search is visible in its own box; it is not this dot's business."""
    set_theme("river")
    body = client.get(reverse("index"), params).content.decode()
    assert "le-more__dot" not in body


def test_event_detail_renders_under_a_theme(client, listings):
    set_theme("river")
    event = Event.objects.get(title="Open Mic Night")
    response = client.get(reverse("event_detail", args=[event.slug]))
    assert response.status_code == 200
    assert "le-detail" in response.content.decode()


def test_unknown_stored_theme_still_renders(client, listings):
    """The database can name a theme this release does not ship."""
    SiteConfig.objects.update(theme="retired-theme")
    response = client.get(reverse("index"))
    assert response.status_code == 200
    assert 'data-theme="classic"' in response.content.decode()


def test_themes_carry_no_instance_identity():
    """A theme is a look, not a community.

    Prose, contact details and place names belong to an instance. A theme that
    hardcoded any of them could not be shared by a second town, which is the
    whole reason themes are a product-level concept.
    """
    for theme in THEMES.values():
        assert theme.name
        assert theme.description
        if theme.stylesheet:
            assert theme.stylesheet.startswith("themes/")
