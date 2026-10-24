"""The moderators' events screen: every listing, and the quick decisions.

The queue judges a submission once. This screen is for the hundred listings
already on the site, which a moderator wants to re-tier, take down or vote for
without opening each one. Each button is one click on a row, so each is
reversible and each writes an audit row — a quick button with no record is
how "who took that down?" becomes a mystery.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from events.models import Event, Interest, Occurrence, Venue
from moderation import services
from submissions.models import ModerationAction


@pytest.fixture
def signed_in_mod(client, moderator):
    client.force_login(moderator)
    return client


@pytest.fixture
def listings(db):
    now = timezone.now()
    hall = Venue.objects.create(name="Community Hall", city="Anytown")
    library = Venue.objects.create(name="Public Library", city="Anytown")

    def make(title, venue, days, **kwargs):
        event = Event.objects.create(
            title=title, venue=venue,
            status=kwargs.pop("status", Event.Status.PUBLISHED),
            prominence=kwargs.pop("prominence", Event.Prominence.LISTED),
            **kwargs,
        )
        if days is not None:
            Occurrence.objects.create(event=event, start=now + timedelta(days=days))
        return event

    return {
        "concert": make("Spring Concert", hall, 3),
        "talk": make("Internet Safety", library, 10, prominence=Event.Prominence.QUIET),
        "gone": make("Winter Fair", hall, -30),
        "pending": make("Not Yet", hall, 5, status=Event.Status.PENDING),
        "undated": make("Someday", hall, None),
    }


HX = {"HTTP_HX_REQUEST": "true"}


# --- the list ----------------------------------------------------------------


def test_the_screen_is_for_moderators(client, submitter, listings):
    assert client.get(reverse("mod_events")).status_code == 302
    client.force_login(submitter)
    assert client.get(reverse("mod_events")).status_code == 403


def test_upcoming_is_the_default_and_hides_the_rest(signed_in_mod, listings):
    response = signed_in_mod.get(reverse("mod_events"))
    titles = [e.title for e in response.context["events"]]
    assert titles == ["Spring Concert", "Internet Safety"]


@pytest.mark.parametrize(
    "view, expected",
    [
        ("past", ["Winter Fair", "Someday"]),
        ("pending", ["Not Yet"]),
        ("removed", []),
        ("all", ["Spring Concert", "Not Yet", "Internet Safety", "Winter Fair", "Someday"]),
    ],
)
def test_each_view_shows_its_own_rows(signed_in_mod, listings, view, expected):
    """Date order puts the undated and the finished last, not first — a null
    sorts ahead of everything on some databases, and nothing ahead of the
    concert on Saturday is the right answer."""
    response = signed_in_mod.get(reverse("mod_events"), {"view": view})
    assert [e.title for e in response.context["events"]] == expected


def test_search_reaches_the_venue(signed_in_mod, listings):
    response = signed_in_mod.get(reverse("mod_events"), {"q": "library"})
    assert [e.title for e in response.context["events"]] == ["Internet Safety"]


def test_tier_filter(signed_in_mod, listings):
    response = signed_in_mod.get(
        reverse("mod_events"), {"tier": Event.Prominence.QUIET}
    )
    assert [e.title for e in response.context["events"]] == ["Internet Safety"]


@pytest.mark.parametrize("sort", list(services.EVENT_SORTS))
def test_every_sort_renders(signed_in_mod, listings, sort):
    response = signed_in_mod.get(reverse("mod_events"), {"view": "all", "sort": sort})
    assert response.status_code == 200
    assert len(response.context["events"]) == 5


def test_sort_by_tier_puts_quiet_below_listed(signed_in_mod, listings):
    response = signed_in_mod.get(reverse("mod_events"), {"sort": "tier"})
    assert [e.title for e in response.context["events"]] == [
        "Spring Concert", "Internet Safety",
    ]


def test_an_unknown_view_or_sort_degrades_rather_than_erroring(signed_in_mod, listings):
    response = signed_in_mod.get(
        reverse("mod_events"), {"view": "nope", "sort": "sideways", "tier": "x"}
    )
    assert response.status_code == 200
    assert response.context["view"] == "upcoming"


def test_htmx_gets_only_the_list(signed_in_mod, listings):
    body = signed_in_mod.get(reverse("mod_events"), **HX).content.decode()
    assert 'id="event-results"' in body
    assert "<html" not in body
    assert "x-data" not in body


# --- the row actions ---------------------------------------------------------


def test_changing_the_tier_swaps_the_row_and_is_recorded(signed_in_mod, listings):
    concert = listings["concert"]
    response = signed_in_mod.post(
        reverse("mod_event_tier", args=[concert.pk]),
        {"tier": Event.Prominence.QUIET},
        **HX,
    )

    assert response.status_code == 200
    concert.refresh_from_db()
    assert concert.prominence == Event.Prominence.QUIET
    action = ModerationAction.objects.get(action="tier_changed", event=concert)
    assert "Listed" in action.detail and "Quiet" in action.detail

    body = response.content.decode()
    assert body.lstrip().startswith(f'<li id="event-{concert.pk}"')
    # The row comes back with its date, not just its title.
    assert "Community Hall" in body
    assert 'value="2" title="Quiet' in body and "aria-current" in body


def test_setting_the_same_tier_records_nothing(signed_in_mod, listings):
    concert = listings["concert"]
    signed_in_mod.post(
        reverse("mod_event_tier", args=[concert.pk]),
        {"tier": Event.Prominence.LISTED},
        **HX,
    )
    assert not ModerationAction.objects.filter(action="tier_changed").exists()


def test_a_missing_tier_is_refused(signed_in_mod, listings):
    response = signed_in_mod.post(
        reverse("mod_event_tier", args=[listings["concert"].pk]), {"tier": "9"}, **HX
    )
    assert response.status_code == 400


def test_interest_is_a_toggle_on_the_moderators_own_mark(signed_in_mod, moderator, listings):
    concert = listings["concert"]
    url = reverse("mod_event_interest", args=[concert.pk])

    body = signed_in_mod.post(url, **HX).content.decode()
    concert.refresh_from_db()
    assert concert.interest_count == 1
    assert Interest.objects.get(event=concert).user == moderator
    assert "&#9829; 1" in body

    signed_in_mod.post(url, **HX)
    concert.refresh_from_db()
    assert concert.interest_count == 0
    assert not Interest.objects.exists()
    # A heart is not a decision: nothing in the log.
    assert not ModerationAction.objects.exists()


def test_unpublish_takes_it_off_the_site_and_republish_puts_it_back(
    signed_in_mod, client, listings
):
    concert = listings["concert"]
    public = reverse("event_detail", args=[concert.slug])
    assert client.get(public).status_code == 200

    body = signed_in_mod.post(
        reverse("mod_event_unpublish", args=[concert.pk]), **HX
    ).content.decode()
    concert.refresh_from_db()
    assert concert.status == Event.Status.UNPUBLISHED
    assert client.get(public).status_code == 404
    assert ">Republish</button>" in body
    assert ">Unpublish</button>" not in body
    assert ModerationAction.objects.filter(action="unpublished", event=concert).exists()

    signed_in_mod.post(reverse("mod_event_republish", args=[concert.pk]), **HX)
    concert.refresh_from_db()
    assert concert.status == Event.Status.PUBLISHED
    assert client.get(public).status_code == 200
    assert ModerationAction.objects.filter(action="republished", event=concert).exists()


def test_marking_a_duplicate_says_so(signed_in_mod, listings):
    concert = listings["concert"]
    signed_in_mod.post(
        reverse("mod_event_unpublish", args=[concert.pk]), {"duplicate": "1"}, **HX
    )
    concert.refresh_from_db()
    assert concert.status == Event.Status.DUPLICATE
    assert ModerationAction.objects.filter(action="marked_duplicate").exists()

    response = signed_in_mod.get(reverse("mod_events"), {"view": "removed"})
    assert [e.title for e in response.context["events"]] == ["Spring Concert"]
    assert "Duplicate" in response.content.decode()


def test_a_removed_listing_is_not_a_possible_twin(signed_in_mod, listings):
    """Taking a duplicate down must also stop it haunting the review screen."""
    from events.duplicates import LIVE_STATUSES

    assert Event.Status.DUPLICATE not in LIVE_STATUSES
    assert Event.Status.UNPUBLISHED not in LIVE_STATUSES


def test_a_second_unpublish_writes_nothing_twice(signed_in_mod, listings):
    url = reverse("mod_event_unpublish", args=[listings["concert"].pk])
    signed_in_mod.post(url, **HX)
    signed_in_mod.post(url, **HX)
    assert ModerationAction.objects.filter(action="unpublished").count() == 1


def test_without_htmx_an_action_redirects_to_the_list(signed_in_mod, listings):
    response = signed_in_mod.post(
        reverse("mod_event_tier", args=[listings["concert"].pk]),
        {"tier": Event.Prominence.FEATURED},
    )
    assert response.status_code == 302
    assert response.url == reverse("mod_events")


def test_row_actions_are_for_moderators(client, submitter, listings):
    client.force_login(submitter)
    for name in ("mod_event_tier", "mod_event_unpublish", "mod_event_republish", "mod_event_interest"):
        response = client.post(reverse(name, args=[listings["concert"].pk]), {"tier": 2})
        assert response.status_code == 403, name
    assert Event.objects.get(pk=listings["concert"].pk).status == Event.Status.PUBLISHED
