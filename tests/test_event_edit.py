"""Correcting a live listing.

The queue judges a submission once. This is the other half of the job: the
wrong time, the moved venue, the typo somebody reports weeks later. It is
reached from the event's own page, so the tests care as much about who sees
the button as about what the form does.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from events.models import Category, Event, Occurrence
from submissions.models import ModerationAction


@pytest.fixture
def event(db):
    event = Event.objects.create(
        title="Pancake Breakfast",
        summary="Pancakes, in the morning.",
        status=Event.Status.PUBLISHED,
        prominence=Event.Prominence.LISTED,
    )
    Occurrence.objects.create(event=event, start=timezone.now() + timedelta(days=7))
    return event


def form_data(event, **overrides):
    """Every field the form renders, since a ModelForm POST is all-or-nothing."""
    data = {
        "title": event.title,
        "summary": event.summary,
        "description": event.description,
        "listing_type": event.listing_type,
        "prominence": event.prominence,
        "status": event.status,
        "price_note": event.price_note,
        "accessibility_notes": event.accessibility_notes,
        "source_url": event.source_url,
        "ticket_url": event.ticket_url,
        "image_url": event.image_url,
    }
    data.update(overrides)
    return data


# --- who gets in -----------------------------------------------------------


def test_the_edit_button_is_on_the_page_for_a_moderator(client, moderator, event):
    client.force_login(moderator)
    body = client.get(reverse("event_detail", args=[event.slug])).content.decode()
    assert reverse("mod_event_edit", args=[event.pk]) in body


def test_a_visitor_never_sees_the_edit_button(client, event):
    body = client.get(reverse("event_detail", args=[event.slug])).content.decode()
    assert reverse("mod_event_edit", args=[event.pk]) not in body


def test_a_signed_in_non_moderator_never_sees_the_edit_button(
    client, submitter, event
):
    client.force_login(submitter)
    body = client.get(reverse("event_detail", args=[event.slug])).content.decode()
    assert reverse("mod_event_edit", args=[event.pk]) not in body


def test_a_moderator_who_is_not_staff_can_open_the_form(client, moderator, event):
    """The whole point of not linking to the Django admin.

    A Moderators-group member has no staff flag and no model permissions, so
    an admin link would have 403'd exactly the people the button is for.
    """
    assert not moderator.is_staff
    client.force_login(moderator)
    response = client.get(reverse("mod_event_edit", args=[event.pk]))
    assert response.status_code == 200


def test_a_non_moderator_is_refused(client, submitter, event):
    client.force_login(submitter)
    response = client.get(reverse("mod_event_edit", args=[event.pk]))
    assert response.status_code == 403


def test_a_logged_out_visitor_gets_the_login_page(client, event):
    response = client.get(reverse("mod_event_edit", args=[event.pk]))
    assert response.status_code == 302
    assert reverse("account_login") in response["Location"]


# --- what it saves ---------------------------------------------------------


def test_an_edit_saves_and_returns_to_the_public_page(client, moderator, event):
    client.force_login(moderator)
    response = client.post(
        reverse("mod_event_edit", args=[event.pk]),
        form_data(event, title="Pancake Brunch", summary="Later than breakfast."),
    )

    event.refresh_from_db()
    assert event.title == "Pancake Brunch"
    assert event.summary == "Later than breakfast."
    assert response["Location"] == reverse("event_detail", args=[event.slug])


def test_the_public_url_survives_a_title_change(client, moderator, event):
    """A slug rewrite would break every link anyone has shared."""
    before = event.slug
    client.force_login(moderator)
    client.post(
        reverse("mod_event_edit", args=[event.pk]),
        form_data(event, title="Something Else Entirely"),
    )

    event.refresh_from_db()
    assert event.slug == before


def test_an_edit_is_written_to_the_audit_log(client, moderator, event):
    client.force_login(moderator)
    client.post(
        reverse("mod_event_edit", args=[event.pk]), form_data(event, title="Fixed")
    )

    action = ModerationAction.objects.get(event=event, action="event_edited")
    assert action.actor == moderator
    assert "title" in action.detail


def test_a_save_that_changes_nothing_is_not_logged(client, moderator, event):
    client.force_login(moderator)
    client.post(reverse("mod_event_edit", args=[event.pk]), form_data(event))

    assert not ModerationAction.objects.filter(action="event_edited").exists()


def test_unpublishing_takes_it_off_the_site(client, moderator, event):
    client.force_login(moderator)
    response = client.post(
        reverse("mod_event_edit", args=[event.pk]),
        form_data(event, status=Event.Status.DRAFT),
    )

    # No public page to send them back to, so they stay on the form.
    assert response["Location"] == reverse("mod_event_edit", args=[event.pk])
    assert client.get(reverse("event_detail", args=[event.slug])).status_code == 404


def test_an_unpublished_event_is_still_editable(client, moderator, event):
    """Otherwise pulling something down for a fix would strand it."""
    event.status = Event.Status.DRAFT
    event.save()

    client.force_login(moderator)
    assert client.get(reverse("mod_event_edit", args=[event.pk])).status_code == 200


def test_prominence_can_be_changed_from_here(client, moderator, event):
    client.force_login(moderator)
    client.post(
        reverse("mod_event_edit", args=[event.pk]),
        form_data(event, prominence=Event.Prominence.FEATURED),
    )

    event.refresh_from_db()
    assert event.prominence == Event.Prominence.FEATURED


# --- what it refuses -------------------------------------------------------


def test_a_backwards_price_range_is_refused(client, moderator, event):
    client.force_login(moderator)
    response = client.post(
        reverse("mod_event_edit", args=[event.pk]),
        form_data(event, price_min="30", price_max="10"),
    )

    assert response.status_code == 200
    assert "below the low price" in response.content.decode()
    event.refresh_from_db()
    assert event.price_min is None


def test_a_backwards_age_range_is_refused(client, moderator, event):
    client.force_login(moderator)
    response = client.post(
        reverse("mod_event_edit", args=[event.pk]),
        form_data(event, age_min="18", age_max="5"),
    )

    assert response.status_code == 200
    assert "below the minimum age" in response.content.decode()


def test_free_and_priced_at_once_is_refused(client, moderator, event):
    """The card would render both a Free chip and a price."""
    client.force_login(moderator)
    response = client.post(
        reverse("mod_event_edit", args=[event.pk]),
        form_data(event, is_free="on", price_min="10"),
    )

    assert response.status_code == 200
    assert "marked free but has a price" in response.content.decode()
    event.refresh_from_db()
    assert not event.is_free


def test_a_blank_title_is_refused(client, moderator, event):
    client.force_login(moderator)
    response = client.post(
        reverse("mod_event_edit", args=[event.pk]), form_data(event, title="")
    )

    assert response.status_code == 200
    event.refresh_from_db()
    assert event.title == "Pancake Breakfast"


# --- categories ------------------------------------------------------------


def test_a_retired_category_is_not_stripped_by_an_unrelated_edit(
    client, moderator, event
):
    """Editing the summary must not quietly drop a category nobody touched."""
    retired = Category.objects.create(
        name="Old Thing", slug="old-thing", is_active=False
    )
    event.categories.add(retired)

    client.force_login(moderator)
    client.post(
        reverse("mod_event_edit", args=[event.pk]),
        form_data(event, summary="A new summary.", categories=[retired.pk]),
    )

    event.refresh_from_db()
    assert event.summary == "A new summary."
    assert list(event.categories.all()) == [retired]


def test_an_active_category_can_be_added(client, moderator, event):
    # A fresh instance ships with seeded categories, so take one of those
    # rather than inventing a slug that may already be taken.
    category = Category.objects.filter(is_active=True).first()
    assert category is not None

    client.force_login(moderator)
    client.post(
        reverse("mod_event_edit", args=[event.pk]),
        form_data(event, categories=[category.pk]),
    )

    event.refresh_from_db()
    assert list(event.categories.all()) == [category]


def test_prices_round_trip(client, moderator, event):
    client.force_login(moderator)
    client.post(
        reverse("mod_event_edit", args=[event.pk]),
        form_data(event, price_min="5.50", price_max="12.00"),
    )

    event.refresh_from_db()
    assert event.price_min == Decimal("5.50")
    assert event.price_max == Decimal("12.00")
