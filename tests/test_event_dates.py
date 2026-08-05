"""Editing when a listing happens.

Until this screen existed the only way to add a second date to a published
event was the Django admin — a staff account, which is a far larger grant than
"may correct a listing". Events entered before an occurrence could carry its
own end time are why it was needed: they hold one date where the source page
always listed six.

The dates deliberately live apart from the rest of the edit form. Everything
there is text a mistake in is embarrassing; a mistake here sends somebody to a
locked hall.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from events.models import Event, Occurrence
from submissions.models import ModerationAction


@pytest.fixture
def event(db):
    event = Event.objects.create(
        title="Harvest Market",
        status=Event.Status.PUBLISHED,
        prominence=Event.Prominence.LISTED,
    )
    Occurrence.objects.create(event=event, start=timezone.now() + timedelta(days=7))
    return event


def local(days=0, hour=10):
    """A local datetime the widget's format can round-trip."""
    return (timezone.localtime() + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


def stamp(value):
    return value.strftime("%Y-%m-%dT%H:%M")


def post_data(rows, total=None, initial=0):
    """A management form plus one block per date row.

    `initial` is how many of the rows the page rendered as existing dates.
    It matters: the formset only offers "Remove this date" on rows it handed
    out, which is `can_delete_extra=False` doing its job — a blank row nobody
    filled in has nothing to remove.
    """
    total = total if total is not None else len(rows)
    data = {
        "dates-TOTAL_FORMS": str(total),
        "dates-INITIAL_FORMS": str(initial),
        "dates-MIN_NUM_FORMS": "0",
        "dates-MAX_NUM_FORMS": "60",
    }
    for index, row in enumerate(rows):
        for field, value in row.items():
            data[f"dates-{index}-{field}"] = value
    return data


# --- who gets in -----------------------------------------------------------


def test_a_visitor_cannot_open_the_dates_screen(client, event):
    response = client.get(reverse("mod_event_dates", args=[event.pk]))
    assert response.status_code in (302, 403)


def test_a_signed_in_non_moderator_cannot_either(client, submitter, event):
    client.force_login(submitter)
    response = client.get(reverse("mod_event_dates", args=[event.pk]))
    assert response.status_code in (302, 403)


def test_a_moderator_who_is_not_staff_can(client, moderator, event):
    """The whole reason this is not a link to the Django admin."""
    assert not moderator.is_staff
    client.force_login(moderator)
    assert client.get(reverse("mod_event_dates", args=[event.pk])).status_code == 200


def test_the_edit_form_links_to_it(client, moderator, event):
    client.force_login(moderator)
    body = client.get(reverse("mod_event_edit", args=[event.pk])).content.decode()
    assert reverse("mod_event_dates", args=[event.pk]) in body


# --- editing ---------------------------------------------------------------


def test_the_existing_dates_are_filled_in(client, moderator, event):
    client.force_login(moderator)
    body = client.get(reverse("mod_event_dates", args=[event.pk])).content.decode()
    occurrence = event.occurrences.get()
    assert stamp(timezone.localtime(occurrence.start)) in body


def test_a_second_date_can_be_added(client, moderator, event):
    """The case the whole screen exists for."""
    client.force_login(moderator)

    client.post(
        reverse("mod_event_dates", args=[event.pk]),
        post_data(
            [
                {"start": stamp(local(7)), "end": stamp(local(7, hour=14))},
                {"start": stamp(local(14)), "end": stamp(local(14, hour=14))},
            ]
        ),
    )

    assert event.occurrences.count() == 2


def test_every_row_keeps_its_own_hours(client, moderator, event):
    """A market open Fridays 9–2 and Saturdays 7–2 must say so.

    An earlier shape had one "ends at" box for a list of bare dates, and every
    closing time but the first was stored as null.
    """
    client.force_login(moderator)

    client.post(
        reverse("mod_event_dates", args=[event.pk]),
        post_data(
            [
                {"start": stamp(local(7, 9)), "end": stamp(local(7, 14))},
                {"start": stamp(local(8, 7)), "end": stamp(local(8, 14))},
            ]
        ),
    )

    ends = [o.end for o in event.occurrences.order_by("start")]
    assert all(end is not None for end in ends)
    assert timezone.localtime(ends[0]).hour == 14
    assert timezone.localtime(ends[1]).hour == 14


def test_an_end_on_a_later_day_is_kept_as_one_date(client, moderator, event):
    """A festival from Friday evening to Sunday afternoon is one occurrence."""
    client.force_login(moderator)

    client.post(
        reverse("mod_event_dates", args=[event.pk]),
        post_data([{"start": stamp(local(7, 18)), "end": stamp(local(9, 17))}]),
    )

    occurrence = event.occurrences.get()
    assert occurrence.spans_days


def test_a_date_can_be_cancelled_without_being_removed(client, moderator, event):
    """Someone with it in their calendar needs to see that it is off."""
    client.force_login(moderator)

    client.post(
        reverse("mod_event_dates", args=[event.pk]),
        post_data([{"start": stamp(local(7)), "is_cancelled": "on"}]),
    )

    assert event.occurrences.get().is_cancelled


def test_a_removed_date_is_gone(client, moderator, event):
    client.force_login(moderator)

    client.post(
        reverse("mod_event_dates", args=[event.pk]),
        post_data(
            [
                {"start": stamp(local(7))},
                {"start": stamp(local(14)), "DELETE": "on"},
            ],
            initial=2,
        ),
    )

    assert event.occurrences.count() == 1


def test_an_end_before_its_start_is_refused(client, moderator, event):
    client.force_login(moderator)

    response = client.post(
        reverse("mod_event_dates", args=[event.pk]),
        post_data([{"start": stamp(local(7, 14)), "end": stamp(local(7, 9))}]),
    )

    assert response.status_code == 200
    assert "must be after the start" in response.content.decode()
    assert event.occurrences.count() == 1


def test_the_same_date_twice_is_refused(client, moderator, event):
    client.force_login(moderator)

    response = client.post(
        reverse("mod_event_dates", args=[event.pk]),
        post_data([{"start": stamp(local(7))}, {"start": stamp(local(7))}]),
    )

    assert "listed twice" in response.content.decode()


def test_a_moderator_may_set_dates_that_have_all_passed(client, moderator, event):
    """The submitter's wrong-year guard is wrong here.

    It is aimed at an extraction the submitter is being asked to rubber-stamp.
    A moderator correcting the record of an event that already happened is
    doing something ordinary, and is looking straight at the year they typed.
    """
    client.force_login(moderator)

    client.post(
        reverse("mod_event_dates", args=[event.pk]),
        post_data([{"start": stamp(local(-30))}]),
    )

    assert event.occurrences.count() == 1
    assert event.occurrences.get().start < timezone.now()


def test_asking_for_more_rows_keeps_what_was_typed(client, moderator, event):
    """The button is a submit, not JavaScript — Alpine cannot run here."""
    client.force_login(moderator)

    response = client.post(
        reverse("mod_event_dates", args=[event.pk]),
        {**post_data([{"start": stamp(local(7))}]), "add_dates": "1"},
    )

    body = response.content.decode()
    assert stamp(local(7)) in body
    assert "dates-2-start" in body
    # Nothing was saved — asking for space is not saying you are finished.
    assert event.occurrences.count() == 1


def test_asking_for_more_rows_does_not_report_errors_yet(client, moderator, event):
    """Nobody asking for another row has said they are done filling this one."""
    client.force_login(moderator)

    response = client.post(
        reverse("mod_event_dates", args=[event.pk]),
        {**post_data([{"start": ""}]), "add_dates": "1"},
    )

    assert "Give at least one date" not in response.content.decode()


def test_saving_writes_an_audit_row(client, moderator, event):
    client.force_login(moderator)

    client.post(
        reverse("mod_event_dates", args=[event.pk]),
        post_data([{"start": stamp(local(7))}]),
    )

    action = ModerationAction.objects.filter(action="dates_edited").get()
    assert action.actor == moderator
    assert action.event == event
