"""Turning a confirmed draft into a real Event.

Kept out of the view so the same path can be reused by the moderation queue and
by tests without going through HTTP.
"""

from django.db import transaction

from events.models import Event
from events.services import organizer_for, set_occurrences, venue_for

from .models import ModerationAction, Submission


@transaction.atomic
def save_event_from_draft(submission, data):
    """Build — or rebuild — the Event a moderator will review.

    Rebuild matters as much as build. When a moderator asks a question the
    submission goes back to its owner with the event already created, and a
    second confirmation has to *update* that event. Creating a fresh one would
    leave the moderator's queue entry pointing at an abandoned row and the
    answer to their question sitting in an event nobody is looking at.

    Status is PENDING, never PUBLISHED: the submitter's confirmation says the
    details are right, not that the event belongs on the site. Prominence is
    never touched here at all, because placement is a moderator's call.
    """
    venue = venue_for(
        data.get("venue_name", ""),
        data.get("venue_address", ""),
        data.get("venue_city", ""),
    )
    organizer = organizer_for(data.get("organizer_name", ""))

    event = submission.event or Event(
        source=Event.Source.SUBMISSION,
        submitted_by=submission.submitted_by,
    )
    is_new = event.pk is None

    event.title = data["title"]
    event.summary = data.get("summary", "")
    event.description = data.get("description", "")
    event.venue = venue
    event.organizer = organizer
    event.source_url = data.get("source_url", "") or submission.source_url
    event.ticket_url = data.get("ticket_url", "")
    event.is_free = data.get("is_free", False)
    event.price_note = data.get("price_note", "")
    event.is_family_friendly = data.get("is_family_friendly", False)
    event.accessibility_notes = data.get("accessibility_notes", "")
    event.listing_type = (
        Event.ListingType.SERIES
        if data.get("is_series")
        else Event.ListingType.ONE_OFF
    )
    event.status = Event.Status.PENDING
    event.save()

    event.categories.set(data.get("categories") or [])

    # Every date carries its own hours. An earlier version wrote `end` onto the
    # first occurrence and null onto the rest, which silently threw away the
    # closing time of every date but one — a market open Fridays 9–2 and
    # Saturdays 7–2 published as "Saturday, 7am" and nothing more.
    set_occurrences(event, data.get("occurrences") or [])

    submission.event = event
    submission.status = Submission.Status.PENDING_REVIEW
    submission.save(update_fields=["event", "status", "updated_at"])

    ModerationAction.record(
        submission.submitted_by,
        "submitted_for_review" if is_new else "resubmitted_for_review",
        submission=submission,
        event=event,
        detail=event.title,
    )

    return event
