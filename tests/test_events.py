"""Domain model behaviour.

The tests worth having here are the ones protecting decisions that are easy to
undo by accident: that collapsing and placement stay separate, that the region
gate flags rather than rejects, and that the interest signal cannot be used to
climb the feed.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from events.models import (
    Category,
    Event,
    GeocodeThrottle,
    Interest,
    Occurrence,
    Organizer,
    Venue,
)


@pytest.fixture
def venue(db):
    return Venue.objects.create(name="Community Hall", city="Anytown")


@pytest.fixture
def event(db, venue):
    return Event.objects.create(
        title="Open Mic Night", venue=venue, status=Event.Status.PUBLISHED
    )


# --- slugs and lifecycle ---------------------------------------------------


@pytest.mark.django_db
def test_slug_is_generated_and_kept_unique(venue):
    a = Event.objects.create(title="Spring Fair")
    b = Event.objects.create(title="Spring Fair")
    assert a.slug == "spring-fair"
    assert b.slug != a.slug
    assert b.slug.startswith("spring-fair-")


@pytest.mark.django_db
def test_published_at_is_stamped_once():
    event = Event.objects.create(title="Talk", status=Event.Status.PUBLISHED)
    first = event.published_at
    assert first is not None

    event.title = "Talk, revised"
    event.save()
    event.refresh_from_db()
    assert event.published_at == first


@pytest.mark.django_db
def test_series_gets_a_renewal_token_but_one_off_does_not():
    series = Event.objects.create(
        title="Weekly Pottery", listing_type=Event.ListingType.SERIES
    )
    one_off = Event.objects.create(title="Gala")
    assert series.renewal_token
    assert one_off.renewal_token == ""


# --- collapsing vs placement ----------------------------------------------


@pytest.mark.django_db
def test_listing_type_and_prominence_are_independent():
    """A weekly market and a weekly class are both series.

    Placement is what separates them, and it is set by a moderator. If these
    two ever get coupled, the Programs section swallows the farmers market.
    """
    market = Event.objects.create(
        title="Farmers Market",
        listing_type=Event.ListingType.SERIES,
        prominence=Event.Prominence.FEATURED,
        status=Event.Status.PUBLISHED,
    )
    class_ = Event.objects.create(
        title="Pottery Class",
        listing_type=Event.ListingType.SERIES,
        prominence=Event.Prominence.BACKGROUND,
        status=Event.Status.PUBLISHED,
    )

    assert market.is_series and class_.is_series

    feed = list(Event.objects.in_main_feed())
    programs = list(Event.objects.programs())

    assert market in feed and market not in programs
    assert class_ in programs and class_ not in feed


@pytest.mark.django_db
def test_main_feed_excludes_unpublished_events():
    Event.objects.create(title="Draft", status=Event.Status.DRAFT)
    Event.objects.create(title="Pending", status=Event.Status.PENDING)
    live = Event.objects.create(title="Live", status=Event.Status.PUBLISHED)

    assert list(Event.objects.in_main_feed()) == [live]


@pytest.mark.django_db
def test_prominence_sorts_most_prominent_first():
    Event.objects.create(title="Background", prominence=Event.Prominence.BACKGROUND,
                         status=Event.Status.PUBLISHED)
    Event.objects.create(title="Featured", prominence=Event.Prominence.FEATURED,
                         status=Event.Status.PUBLISHED)
    Event.objects.create(title="Listed", prominence=Event.Prominence.LISTED,
                         status=Event.Status.PUBLISHED)

    titles = [e.title for e in Event.objects.published()]
    assert titles == ["Featured", "Listed", "Background"]


# --- occurrences -----------------------------------------------------------


@pytest.mark.django_db
def test_next_occurrence_skips_past_and_cancelled(event):
    now = timezone.now()
    Occurrence.objects.create(event=event, start=now - timedelta(days=1))
    cancelled = Occurrence.objects.create(
        event=event, start=now + timedelta(days=1), is_cancelled=True
    )
    upcoming = Occurrence.objects.create(event=event, start=now + timedelta(days=2))

    assert event.next_occurrence() == upcoming
    assert cancelled not in event.upcoming_occurrences()


@pytest.mark.django_db
def test_an_event_cannot_have_two_occurrences_at_the_same_instant(event):
    from django.db import IntegrityError

    start = timezone.now() + timedelta(days=3)
    Occurrence.objects.create(event=event, start=start)
    with pytest.raises(IntegrityError):
        Occurrence.objects.create(event=event, start=start)


# --- region gate -----------------------------------------------------------


@pytest.mark.django_db
def test_region_gate_flags_rather_than_rejects(settings, venue):
    """Outside the bounds is a moderator's decision, not an automatic no.

    Communities care about things just over the county line, so this must
    surface for review rather than silently drop the event.
    """
    settings.MAP_BBOX = [43.0, -80.5, 43.3, -80.0]

    venue.latitude, venue.longitude = 43.1, -80.2  # inside
    venue.save()
    inside = Event.objects.create(title="Inside", venue=venue)
    assert inside.needs_region_review() is False

    venue.latitude, venue.longitude = 51.5, -0.1  # far outside
    venue.save()
    outside = Event.objects.create(title="Outside", venue=venue)
    assert outside.needs_region_review() is True
    # ...and it still exists, rather than having been refused.
    assert Event.objects.filter(pk=outside.pk).exists()


@pytest.mark.django_db
def test_online_events_are_never_region_flagged(settings):
    settings.MAP_BBOX = [43.0, -80.5, 43.3, -80.0]
    online = Event.objects.create(title="Webinar", is_online=True)
    assert online.needs_region_review() is False


# --- geocode throttle ------------------------------------------------------


@pytest.mark.django_db
def test_throttle_requires_no_wait_when_never_called():
    assert GeocodeThrottle.seconds_to_wait() == 0.0


@pytest.mark.django_db
def test_throttle_makes_a_second_caller_wait():
    """The whole point: back-to-back calls are spaced by a second."""
    slept = []
    GeocodeThrottle.acquire(sleep=slept.append)
    assert slept == []  # first call is free

    GeocodeThrottle.acquire(sleep=slept.append)
    assert len(slept) == 1
    assert 0 < slept[0] <= 1.0


@pytest.mark.django_db
def test_throttle_does_not_wait_once_the_interval_has_passed():
    GeocodeThrottle.acquire(sleep=lambda s: None)
    row = GeocodeThrottle.objects.get(pk=1)
    row.last_called_at = timezone.now() - timedelta(seconds=5)
    row.save()

    slept = []
    GeocodeThrottle.acquire(sleep=slept.append)
    assert slept == []


# --- interest --------------------------------------------------------------


@pytest.mark.django_db
def test_interest_count_tracks_marks(event, django_user_model):
    user = django_user_model.objects.create_user(
        username="a", email="a@example.com", password="pw"
    )
    interest = Interest.objects.create(event=event, user=user)
    event.refresh_from_db()
    assert event.interest_count == 1

    interest.delete()
    event.refresh_from_db()
    assert event.interest_count == 0


@pytest.mark.django_db
def test_a_user_cannot_register_interest_twice(event, django_user_model):
    from django.db import IntegrityError

    user = django_user_model.objects.create_user(
        username="a", email="a@example.com", password="pw"
    )
    Interest.objects.create(event=event, user=user)
    with pytest.raises(IntegrityError):
        Interest.objects.create(event=event, user=user)


@pytest.mark.django_db
def test_anonymous_interest_is_deduplicated_by_fingerprint(event):
    from django.db import IntegrityError

    Interest.objects.create(event=event, fingerprint="abc123")
    with pytest.raises(IntegrityError):
        Interest.objects.create(event=event, fingerprint="abc123")


@pytest.mark.django_db
def test_interest_cannot_change_an_events_prominence(event):
    """The crowd nominates; a human decides.

    This is the property that makes weak anonymous deduplication acceptable —
    inflating the count cannot promote anything, it can only put an event in
    front of a moderator.
    """
    before = event.prominence
    for i in range(500):
        Interest.objects.create(event=event, fingerprint=f"fp-{i}")

    event.refresh_from_db()
    assert event.interest_count == 500
    assert event.prominence == before
    assert event not in Event.objects.filter(prominence=Event.Prominence.FEATURED)


# --- organizers and series caps -------------------------------------------


@pytest.mark.django_db
def test_organizer_series_cap_is_enforceable():
    organizer = Organizer.objects.create(name="Studio", max_active_series=2)
    for i in range(2):
        Event.objects.create(
            title=f"Class {i}",
            organizer=organizer,
            listing_type=Event.ListingType.SERIES,
            status=Event.Status.PUBLISHED,
        )
    assert organizer.active_series_count() == 2
    assert organizer.has_series_capacity() is False


@pytest.mark.django_db
def test_one_off_events_do_not_count_against_the_series_cap():
    organizer = Organizer.objects.create(name="Studio", max_active_series=1)
    Event.objects.create(
        title="Gala", organizer=organizer, status=Event.Status.PUBLISHED
    )
    assert organizer.has_series_capacity() is True


# --- series lifecycle ------------------------------------------------------


@pytest.mark.django_db
def test_expired_series_are_identified():
    today = timezone.now().date()
    stale = Event.objects.create(
        title="Old Class",
        listing_type=Event.ListingType.SERIES,
        status=Event.Status.PUBLISHED,
        series_ends_on=today - timedelta(days=1),
    )
    current = Event.objects.create(
        title="Current Class",
        listing_type=Event.ListingType.SERIES,
        status=Event.Status.PUBLISHED,
        series_ends_on=today + timedelta(days=30),
    )

    expired = list(Event.objects.expired_series())
    assert stale in expired
    assert current not in expired


@pytest.mark.django_db
def test_series_needing_renewal_excludes_already_reminded():
    today = timezone.now().date()
    due = Event.objects.create(
        title="Due",
        listing_type=Event.ListingType.SERIES,
        status=Event.Status.PUBLISHED,
        series_ends_on=today + timedelta(days=3),
    )
    reminded = Event.objects.create(
        title="Reminded",
        listing_type=Event.ListingType.SERIES,
        status=Event.Status.PUBLISHED,
        series_ends_on=today + timedelta(days=3),
        last_renewal_email_at=timezone.now(),
    )

    needing = list(Event.objects.needing_renewal())
    assert due in needing
    assert reminded not in needing


# --- categories ------------------------------------------------------------


@pytest.mark.django_db
def test_categories_are_seeded_and_place_neutral():
    assert Category.objects.count() >= 10
    for category in Category.objects.all():
        assert category.slug
        assert category.emoji


@pytest.mark.django_db
def test_price_display_prefers_free_then_note_then_range():
    free = Event.objects.create(title="Free", is_free=True)
    noted = Event.objects.create(title="Noted", price_note="Pay what you can")
    ranged = Event.objects.create(title="Ranged", price_min=10, price_max=25)
    silent = Event.objects.create(title="Silent")

    assert free.price_display == "Free"
    assert noted.price_display == "Pay what you can"
    assert "10" in ranged.price_display and "25" in ranged.price_display
    assert silent.price_display == ""
