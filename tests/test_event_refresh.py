"""Re-reading a listing's source page.

Two things this covers, and they are the same feature from two ends. A
moderator can now edit an event's dates without a staff account, and can ask
the site to read the source page again and *offer* what changed.

The tests that matter most are the ones about restraint: a refresh must never
write to a published listing on its own, never empty a field because the page
went quiet, never move an event's placement, and never delete the dates an
event has already run. Each of those is a way this feature could quietly
damage listings a human had already checked.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from enrichment.models import EnrichmentRun
from enrichment.pipeline import EnrichmentResult
from enrichment.schemas import EventDraft
from events.models import Category, Event, EventRefresh, Occurrence, Venue
from events.refresh import (
    apply_refresh,
    changes_for,
    refresh_event_from_source,
    request_refresh,
)
from submissions.models import ModerationAction


@pytest.fixture
def event(db):
    event = Event.objects.create(
        title="Harvest Market",
        summary="A market.",
        description="Stalls and produce.",
        status=Event.Status.PUBLISHED,
        prominence=Event.Prominence.LISTED,
        source_url="https://example.org/market",
    )
    # The listing as an older version of the site recorded it: one date, no
    # end time, where the page has always shown a run of them.
    Occurrence.objects.create(event=event, start=timezone.now() + timedelta(days=7))
    return event


def draft(**overrides):
    """An extraction result, with only what a test cares about set."""
    base = {"title": "Harvest Market"}
    base.update(overrides)
    return EventDraft(**base)


@pytest.fixture
def reads_page(monkeypatch):
    """Make the pipeline return a chosen draft instead of touching the network.

    The fetcher is an SSRF boundary and the model call costs money; neither
    belongs in a unit test. Patched at `enrich_url` because that is the seam
    the refresh task imports.
    """

    def use(result_draft, method="structured", message="Read it."):
        def fake(url, submission=None, event=None):
            # Keep the cost record the real pipeline would have written, since
            # a test below asserts the refresh path lands in the same table.
            EnrichmentRun.objects.create(
                submission=submission,
                event=event,
                source_url=url,
                method=method,
                status=EnrichmentRun.Status.OK,
            )
            return EnrichmentResult(
                draft=result_draft, method=method, message=message
            )

        monkeypatch.setattr("enrichment.pipeline.enrich_url", fake)

    return use


def ready_refresh(event, result_draft, **kwargs):
    """Run a refresh to completion and hand back the record."""
    return EventRefresh.objects.create(
        event=event,
        source_url=event.source_url,
        status=EventRefresh.Status.READY,
        draft=result_draft.model_dump(mode="json"),
        method="structured",
        message="Read it.",
        **kwargs,
    )


def dates_at(*offsets):
    """Occurrence dicts a whole number of days from now, on the hour."""
    now = timezone.now().replace(microsecond=0, second=0)
    return [{"start": now + timedelta(days=n)} for n in offsets]


# --- running the read ------------------------------------------------------


def test_the_task_parks_a_draft_for_review(db, event, reads_page):
    reads_page(draft(summary="Now with forty stalls."))

    refresh = request_refresh(event)
    refresh.refresh_from_db()

    assert refresh.status == EventRefresh.Status.READY
    assert refresh.draft["summary"] == "Now with forty stalls."


def test_the_read_never_touches_the_event_itself(db, event, reads_page):
    """The whole point. A proposal is not a change."""
    reads_page(draft(summary="Now with forty stalls.", occurrences=dates_at(1, 2, 3)))

    request_refresh(event)
    event.refresh_from_db()

    assert event.summary == "A market."
    assert event.occurrences.count() == 1


def test_the_cost_of_a_refresh_lands_on_the_event(db, event, reads_page):
    """Otherwise a re-read is an orphan row nobody can account for."""
    reads_page(draft())

    request_refresh(event)

    run = EnrichmentRun.objects.get()
    assert run.event == event
    assert run.submission is None


def test_a_second_request_joins_the_one_already_running(db, event):
    first = request_refresh(event)
    # Nothing has completed it, so it is still in flight.
    EventRefresh.objects.filter(pk=first.pk).update(
        status=EventRefresh.Status.READING
    )

    second = request_refresh(event)

    assert second.pk == first.pk
    assert EventRefresh.objects.count() == 1


def test_a_new_read_supersedes_a_proposal_nobody_acted_on(db, event, reads_page):
    """Otherwise the queue holds two readings of one page for one listing."""
    stale = ready_refresh(event, draft(summary="An older reading."))
    reads_page(draft(summary="Now with forty stalls."))

    request_refresh(event)

    stale.refresh_from_db()
    assert stale.status == EventRefresh.Status.DISCARDED
    assert EventRefresh.objects.awaiting_review().count() == 1


def test_a_failed_read_says_so_rather_than_spinning(db, event, monkeypatch):
    def fake(url, submission=None, event=None):
        return EnrichmentResult(message="That page can't be read.", failed=True)

    monkeypatch.setattr("enrichment.pipeline.enrich_url", fake)

    refresh = request_refresh(event)
    refresh.refresh_from_db()

    assert refresh.status == EventRefresh.Status.FAILED
    assert "can't be read" in refresh.message


def test_a_crash_in_the_pipeline_does_not_strand_the_refresh(db, event, monkeypatch):
    def explode(url, submission=None, event=None):
        raise RuntimeError("boom")

    monkeypatch.setattr("enrichment.pipeline.enrich_url", explode)

    refresh = request_refresh(event)
    refresh.refresh_from_db()

    assert refresh.status == EventRefresh.Status.FAILED


def test_a_worker_that_died_is_given_up_on(db, event):
    refresh = EventRefresh.objects.create(
        event=event, source_url=event.source_url, status=EventRefresh.Status.READING
    )
    EventRefresh.objects.filter(pk=refresh.pk).update(
        updated_at=timezone.now() - timedelta(hours=2)
    )
    refresh.refresh_from_db()

    assert refresh.is_stranded
    refresh.give_up()
    assert refresh.status == EventRefresh.Status.FAILED


def test_a_vanished_refresh_does_not_crash_the_worker(db):
    """One un-importable or unresolvable task takes down every queued job."""
    refresh_event_from_source.enqueue(999_999)


# --- what counts as a change -----------------------------------------------


def test_an_unchanged_page_proposes_nothing(db, event):
    refresh = ready_refresh(
        event, draft(summary="A market.", description="Stalls and produce.")
    )
    assert changes_for(refresh) == []


def test_a_new_summary_is_offered_with_the_old_one_beside_it(db, event):
    refresh = ready_refresh(event, draft(summary="Now with forty stalls."))

    change = next(c for c in changes_for(refresh) if c.key == "summary")
    assert change.current == ["A market."]
    assert change.proposed == ["Now with forty stalls."]


def test_a_page_that_went_quiet_never_proposes_emptying_a_field(db, event):
    """A redesigned page extracts as a title and little else.

    Reading that silence as "the description is gone now" would gut every
    listing whose source moved to a JavaScript shell.
    """
    refresh = ready_refresh(event, draft(summary="", description=""))

    assert [c.key for c in changes_for(refresh)] == []


def test_a_flag_is_only_ever_proposed_as_true(db, event):
    """The schema defaults these to False, so a False means "didn't say"."""
    event.is_free = True
    event.save()

    refresh = ready_refresh(event, draft(is_free=False))

    assert [c.key for c in changes_for(refresh)] == []


def test_free_is_not_proposed_over_a_price(db, event):
    """It is the contradiction the edit form rejects; don't create it here."""
    event.price_min = 5
    event.save()

    refresh = ready_refresh(event, draft(is_free=True))

    assert "is_free" not in [c.key for c in changes_for(refresh)]


def test_categories_are_added_to_and_never_replaced(db, event):
    # get_or_create because a migration seeds a starter set of categories.
    music, _ = Category.objects.get_or_create(slug="music", defaults={"name": "Music"})
    food, _ = Category.objects.get_or_create(slug="food", defaults={"name": "Food"})
    event.categories.add(music)

    refresh = ready_refresh(event, draft(category_slugs=["food"]))
    change = next(c for c in changes_for(refresh) if c.key == "categories")

    assert set(change.proposed) == {music.name, food.name}

    apply_refresh(refresh, ["categories"])
    assert set(event.categories.all()) == {music, food}


def test_the_dates_a_page_lists_are_offered_as_a_set(db, event):
    refresh = ready_refresh(event, draft(occurrences=dates_at(3, 10, 17)))

    change = next(c for c in changes_for(refresh) if c.key == "occurrences")
    assert len(change.proposed) == 3


def test_dates_that_have_all_gone_by_carry_a_warning(db, event):
    """The model's standing habit is to date an undated page to last year."""
    refresh = ready_refresh(event, draft(occurrences=dates_at(-400, -393)))

    change = next(c for c in changes_for(refresh) if c.key == "occurrences")
    assert "year" in change.warning


# --- taking it -------------------------------------------------------------


def test_applying_writes_only_what_was_ticked(db, event, moderator):
    refresh = ready_refresh(
        event,
        draft(summary="Now with forty stalls.", description="A different blurb."),
    )

    apply_refresh(refresh, ["summary"], moderator)
    event.refresh_from_db()

    assert event.summary == "Now with forty stalls."
    assert event.description == "Stalls and produce."


def test_applying_never_moves_a_listing(db, event, moderator):
    """A page has no opinion about where an event sits or whether it is up."""
    refresh = ready_refresh(event, draft(summary="Now with forty stalls."))
    before = (event.status, event.prominence, event.slug, event.listing_type)

    apply_refresh(refresh, ["summary"], moderator)
    event.refresh_from_db()

    assert (event.status, event.prominence, event.slug, event.listing_type) == before


def test_applying_dates_replaces_the_future_and_keeps_the_past(db, event, moderator):
    """A source page describes what is coming, not what already happened.

    Without this a weekly class re-read in its ninth month would lose every
    date it had ever run.
    """
    ran = timezone.now() - timedelta(days=30)
    Occurrence.objects.create(event=event, start=ran)

    refresh = ready_refresh(event, draft(occurrences=dates_at(3, 10, 17)))
    apply_refresh(refresh, ["occurrences"], moderator)

    starts = list(event.occurrences.order_by("start").values_list("start", flat=True))
    assert starts[0] == ran
    assert len(starts) == 4


def test_applying_dates_leaves_a_cancellation_alone(db, event, moderator):
    """A refresh says nothing about cancellation, so it must not undo one."""
    when = timezone.now() + timedelta(days=3)
    Occurrence.objects.create(event=event, start=when, is_cancelled=True)

    refresh = ready_refresh(event, draft(occurrences=[{"start": when}]))
    apply_refresh(refresh, ["occurrences"], moderator)

    assert event.occurrences.get(start=when).is_cancelled


def test_applying_a_venue_reuses_the_one_already_on_the_map(db, event, moderator):
    hall = Venue.objects.create(name="Community Hall", city="Riverton")

    refresh = ready_refresh(
        event, draft(venue_name="community hall", venue_city="riverton")
    )
    apply_refresh(refresh, ["venue"], moderator)
    event.refresh_from_db()

    assert event.venue == hall
    assert Venue.objects.count() == 1


def test_what_was_taken_is_recorded_on_the_refresh(db, event, moderator):
    refresh = ready_refresh(event, draft(summary="Now with forty stalls."))

    apply_refresh(refresh, ["summary"], moderator)
    refresh.refresh_from_db()

    assert refresh.status == EventRefresh.Status.APPLIED
    assert refresh.applied_fields == ["Summary"]
    assert refresh.applied_by == moderator


# --- the screens -----------------------------------------------------------


def test_a_visitor_cannot_start_a_refresh(client, event):
    response = client.post(
        reverse("mod_event_refresh", args=[event.pk]), {"start": "1"}
    )
    assert response.status_code in (302, 403)
    assert not EventRefresh.objects.exists()


def test_a_moderator_starts_one_from_the_listing(client, moderator, event, reads_page):
    reads_page(draft(summary="Now with forty stalls."))
    client.force_login(moderator)

    client.post(reverse("mod_event_refresh", args=[event.pk]), {"start": "1"})

    assert EventRefresh.objects.get().status == EventRefresh.Status.READY


def test_the_review_page_shows_both_values(client, moderator, event):
    ready_refresh(event, draft(summary="Now with forty stalls."))
    client.force_login(moderator)

    body = client.get(reverse("mod_event_refresh", args=[event.pk])).content.decode()

    assert "A market." in body
    assert "Now with forty stalls." in body


def test_taking_a_change_through_the_page_writes_an_audit_row(
    client, moderator, event
):
    ready_refresh(event, draft(summary="Now with forty stalls."))
    client.force_login(moderator)

    client.post(
        reverse("mod_event_refresh", args=[event.pk]), {"summary": "on"}
    )

    event.refresh_from_db()
    assert event.summary == "Now with forty stalls."
    assert ModerationAction.objects.filter(action="refreshed_from_source").exists()


def test_ticking_nothing_changes_nothing(client, moderator, event):
    ready_refresh(event, draft(summary="Now with forty stalls."))
    client.force_login(moderator)

    client.post(reverse("mod_event_refresh", args=[event.pk]), {})

    event.refresh_from_db()
    assert event.summary == "A market."


def test_discarding_leaves_the_listing_alone_and_clears_the_queue(
    client, moderator, event
):
    refresh = ready_refresh(event, draft(summary="Now with forty stalls."))
    client.force_login(moderator)

    client.post(reverse("mod_event_refresh", args=[event.pk]), {"discard": "1"})

    event.refresh_from_db()
    refresh.refresh_from_db()
    assert event.summary == "A market."
    assert refresh.status == EventRefresh.Status.DISCARDED
    assert not EventRefresh.objects.awaiting_review().exists()


def test_a_listing_with_no_source_page_says_so(client, moderator, event):
    event.source_url = ""
    event.save()
    client.force_login(moderator)

    body = client.get(reverse("mod_event_refresh", args=[event.pk])).content.decode()

    assert "no source page" in body
    client.post(reverse("mod_event_refresh", args=[event.pk]), {"start": "1"})
    assert not EventRefresh.objects.exists()


def test_a_read_in_flight_shows_a_spinner_that_polls(client, moderator, event):
    EventRefresh.objects.create(
        event=event, source_url=event.source_url, status=EventRefresh.Status.READING
    )
    client.force_login(moderator)

    body = client.get(reverse("mod_event_refresh", args=[event.pk])).content.decode()

    assert "Reading the page" in body
    assert reverse("mod_event_refresh_status", args=[event.pk]) in body


def test_the_poll_target_redirects_once_the_read_is_done(client, moderator, event):
    ready_refresh(event, draft(summary="Now with forty stalls."))
    client.force_login(moderator)

    response = client.get(reverse("mod_event_refresh_status", args=[event.pk]))

    assert response["HX-Redirect"] == reverse("mod_event_refresh", args=[event.pk])


def test_the_poll_target_gives_up_on_a_dead_worker(client, moderator, event):
    """Otherwise the spinner turns forever with nothing behind it."""
    refresh = EventRefresh.objects.create(
        event=event, source_url=event.source_url, status=EventRefresh.Status.READING
    )
    EventRefresh.objects.filter(pk=refresh.pk).update(
        updated_at=timezone.now() - timedelta(hours=2)
    )
    client.force_login(moderator)

    response = client.get(reverse("mod_event_refresh_status", args=[event.pk]))

    refresh.refresh_from_db()
    assert refresh.status == EventRefresh.Status.FAILED
    assert "HX-Redirect" in response


def test_the_queue_lists_what_is_waiting(client, moderator, event):
    ready_refresh(event, draft(summary="Now with forty stalls."))
    client.force_login(moderator)

    body = client.get(reverse("mod_refreshes")).content.decode()

    assert "Harvest Market" in body
    assert reverse("mod_event_refresh", args=[event.pk]) in body
