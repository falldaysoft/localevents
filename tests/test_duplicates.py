"""Telling a moderator the listing may already be on the site.

Two copies of one fair were both published: each submission looked fine on
its own and nothing put them side by side. The check warns and never blocks,
so the tests hold both halves — the twin is found, and near-misses are not.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from events.duplicates import possible_duplicates, titles_match
from events.models import Event, Occurrence, Venue
from submissions.models import Submission


def _event(title, start, end=None, status=Event.Status.PUBLISHED, **kwargs):
    event = Event.objects.create(title=title, status=status, **kwargs)
    Occurrence.objects.create(event=event, start=start, end=end)
    return event


@pytest.fixture
def saturday():
    return (timezone.now() + timedelta(days=10)).replace(hour=12, minute=0, second=0, microsecond=0)


# --- titles --------------------------------------------------------------------


@pytest.mark.parametrize(
    "a, b",
    [
        ("Le Petit Renaissance Faire", "Le Petit Renaissance Faire"),
        ("Le Petit Renaissance Faire", "le petit renaissance faire!"),
        ("Le Petit Renaissance Faire", "Petit Renaissance Faire at Le Petit Marché"),
        ("Café Night", "Cafe Night"),
        ("Fall Fair 2026", "The Fall Fair"),
    ],
)
def test_the_same_event_written_two_ways_matches(a, b):
    assert titles_match(a, b)


@pytest.mark.parametrize(
    "a, b",
    [
        ("Open Mic Night", "Trivia Night"),
        ("Farmers Market", "Christmas Craft Market"),
        ("Book Club", "Chess Club"),
    ],
)
def test_different_events_do_not(a, b):
    assert not titles_match(a, b)


# --- finding the twin -------------------------------------------------------------


@pytest.mark.django_db
def test_the_renaissance_faire_case(saturday):
    """The pair that got through: one with a venue and one without, one a
    single afternoon and the other the whole weekend."""
    hall = Venue.objects.create(name="Le Petit Marché")
    first = _event("Le Petit Renaissance Faire", saturday, saturday + timedelta(hours=7), venue=hall)
    second = _event(
        "Le Petit Renaissance Faire", saturday, saturday + timedelta(days=1, hours=5),
        status=Event.Status.PENDING,
    )

    assert [m["event"] for m in possible_duplicates(second)] == [first]
    assert [m["event"] for m in possible_duplicates(first)] == [second]


@pytest.mark.django_db
def test_the_same_title_on_a_different_weekend_is_a_different_event(saturday):
    _event("Open Mic Night", saturday)
    later = _event("Open Mic Night", saturday + timedelta(days=14), status=Event.Status.PENDING)
    assert possible_duplicates(later) == []


@pytest.mark.django_db
def test_a_day_of_slack_catches_a_wrong_start_time(saturday):
    first = _event("Harvest Supper", saturday)
    second = _event("Harvest Supper", saturday + timedelta(hours=20), status=Event.Status.PENDING)
    assert [m["event"] for m in possible_duplicates(second)] == [first]


@pytest.mark.django_db
def test_rejected_and_draft_events_are_not_offered(saturday):
    _event("Harvest Supper", saturday, status=Event.Status.REJECTED)
    _event("Harvest Supper", saturday, status=Event.Status.DRAFT)
    second = _event("Harvest Supper", saturday, status=Event.Status.PENDING)
    assert possible_duplicates(second) == []


@pytest.mark.django_db
def test_the_same_source_page_matches_whatever_the_title(saturday):
    first = _event("Autumn Makers Market", saturday, source_url="https://www.example.org/events/fair/")
    second = _event(
        "Saturday Artisans", saturday + timedelta(days=30),
        status=Event.Status.PENDING, source_url="http://example.org/events/fair",
    )
    matches = possible_duplicates(second)
    assert [m["event"] for m in matches] == [first]
    assert matches[0]["reason"] == "Same source page"


# --- what the moderator sees ---------------------------------------------------------


@pytest.fixture
def pending_twin(saturday, submitter):
    _event("Le Petit Renaissance Faire", saturday)
    event = _event("Le Petit Renaissance Faire", saturday, status=Event.Status.PENDING)
    return Submission.objects.create(
        submitted_by=submitter, status=Submission.Status.PENDING_REVIEW, event=event,
    )


@pytest.mark.django_db
def test_the_review_screen_warns_and_still_offers_approval(client, moderator, pending_twin):
    client.force_login(moderator)
    body = client.get(reverse("mod_submission", args=[pending_twin.pk])).content.decode()
    assert "This may already be listed." in body
    assert reverse("mod_approve", args=[pending_twin.pk]) in body


@pytest.mark.django_db
def test_the_queue_marks_the_submission(client, moderator, pending_twin):
    client.force_login(moderator)
    body = client.get(reverse("mod_queue")).content.decode()
    assert "Possible duplicate" in body


@pytest.mark.django_db
def test_no_warning_without_a_twin(client, moderator, submitter, saturday):
    event = _event("Harvest Supper", saturday, status=Event.Status.PENDING)
    submission = Submission.objects.create(
        submitted_by=submitter, status=Submission.Status.PENDING_REVIEW, event=event,
    )
    client.force_login(moderator)
    assert "This may already be listed." not in client.get(
        reverse("mod_submission", args=[submission.pk])
    ).content.decode()
    assert "Possible duplicate" not in client.get(reverse("mod_queue")).content.decode()
